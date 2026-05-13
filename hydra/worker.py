"""Single research-job lifecycle: spawn a CLI, validate output, write artifact.

Plan Section 4.5.5: pick a model via the quota router, prompt it with the
flag's context, parse noisy stdout, validate citations, and persist a
``q-NNN.md`` artifact under ``<session>/hydra/research/``.

Two retry counters travel in parallel: ``auto_retried`` covers a single
timeout retry for the deep tier; ``_429_retries`` counts distinct providers
blacklisted mid-flight (capped at 3 to stop infinite rotation).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from hydra import citations, json_extract, state, subprocess_runner
from hydra.models import ModelSpec, Tier
from hydra.quota import NoCandidateModelError, QuotaRouter
from hydra.watcher import Flag

logger = logging.getLogger("hydra.worker")

JobStatus = Literal["queued", "running", "done", "failed", "timeout"]

_MAX_429_RETRIES = 3
_SIGTERM_GRACE_S = 0.2


@dataclass
class ResearchJob:
    q_id: str
    tier: Tier
    flag: Flag
    session_dir: Path
    meeting_context: str = ""
    status: JobStatus = "queued"
    model_spec: ModelSpec | None = None
    started_at: float | None = None
    completed_at: float | None = None
    duration_s: float | None = None
    error: str | None = None
    artifact_path: Path | None = None
    auto_retried: bool = False


@dataclass
class WorkerEvent:
    type: str
    job: ResearchJob
    extra: dict = field(default_factory=dict)


WorkerEventSink = Callable[[WorkerEvent], Awaitable[None]] | None
WorkerSpawn = Callable[..., Awaitable[asyncio.subprocess.Process]]


async def run_research_job(
    job: ResearchJob,
    *,
    quota_router: QuotaRouter,
    on_event: WorkerEventSink = None,
    spawn: WorkerSpawn | None = None,
    clock: Callable[[], float] | None = None,
) -> ResearchJob:
    """Run one research job to completion. Mutates and returns the job."""
    spawn_fn = spawn if spawn is not None else subprocess_runner.spawn
    clock_fn = clock if clock is not None else time.monotonic

    job.started_at = clock_fn()
    job.status = "running"
    await _emit(on_event, "job_started", job)

    blacklisted_providers: list[str] = []

    try:
        try:
            chosen = quota_router.pick_model(job.tier)
        except NoCandidateModelError as exc:
            return await _finalize_failure(
                job, clock_fn, f"no candidate model: {exc}", on_event=on_event
            )

        while True:
            job.model_spec = chosen
            outcome = await _run_once(
                job=job,
                model_spec=chosen,
                spawn_fn=spawn_fn,
            )

            if outcome.kind == "success":
                return await _finalize_success(
                    job=job,
                    clock_fn=clock_fn,
                    model_spec=chosen,
                    validated=outcome.validated,
                    on_event=on_event,
                )

            if outcome.kind == "timeout":
                if job.tier == "heavy" and not job.auto_retried:
                    job.auto_retried = True
                    try:
                        retry_spec = _pick_different(
                            quota_router, job.tier, exclude=chosen
                        )
                    except NoCandidateModelError as exc:
                        return await _finalize_failure(
                            job,
                            clock_fn,
                            f"auto-retry exhausted: {exc}",
                            status="timeout",
                            on_event=on_event,
                            event_type="job_timeout",
                        )
                    chosen = retry_spec
                    continue
                return await _finalize_failure(
                    job,
                    clock_fn,
                    "auto-retry exhausted" if job.auto_retried else "hard timeout",
                    status="timeout",
                    on_event=on_event,
                    event_type="job_timeout",
                )

            if outcome.kind == "rate_limited":
                provider = chosen.cli
                quota_router.mark_blacklisted(provider)
                if provider not in blacklisted_providers:
                    blacklisted_providers.append(provider)
                await _emit(
                    on_event,
                    "provider_blacklisted",
                    job,
                    extra={"provider": provider},
                )
                if len(blacklisted_providers) >= _MAX_429_RETRIES:
                    return await _finalize_failure(
                        job,
                        clock_fn,
                        f"tier exhausted: {len(blacklisted_providers)} "
                        f"providers blacklisted",
                        on_event=on_event,
                    )
                try:
                    chosen = quota_router.pick_model(job.tier)
                except NoCandidateModelError as exc:
                    return await _finalize_failure(
                        job,
                        clock_fn,
                        f"tier exhausted: {exc}",
                        on_event=on_event,
                    )
                continue

            return await _finalize_failure(
                job,
                clock_fn,
                outcome.error,
                on_event=on_event,
            )
    finally:
        if job.completed_at is None:
            job.completed_at = clock_fn()
            if job.started_at is not None:
                job.duration_s = job.completed_at - job.started_at


@dataclass
class _Outcome:
    kind: Literal["success", "failure", "timeout", "rate_limited"]
    error: str = ""
    validated: citations.ValidatedAnswer | None = None


async def _run_once(
    *,
    job: ResearchJob,
    model_spec: ModelSpec,
    spawn_fn: WorkerSpawn,
) -> _Outcome:
    prompt = _build_prompt(job, model_spec)
    argv = _build_argv(model_spec, prompt)
    label = f"worker:{job.q_id}:{model_spec.to_id()}"

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await spawn_fn(argv, label=label)
    except Exception as exc:
        return _Outcome(kind="failure", error=f"spawn failed: {exc}")

    try:
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=model_spec.hard_timeout_s,
            )
        except TimeoutError:
            await _terminate_proc(proc)
            return _Outcome(kind="timeout", error="hard timeout")

        stdout = _decode(stdout_b)
        stderr = _decode(stderr_b)

        rc = proc.returncode

        if _detect_429(stdout, stderr):
            return _Outcome(
                kind="rate_limited",
                error=f"rate-limit signal in output (rc={rc})",
            )

        if rc != 0:
            tail = stderr[-500:] if stderr else stdout[-500:]
            return _Outcome(kind="failure", error=f"exit {rc}: {tail}")

        try:
            payload = json_extract.extract_json(stdout)
        except json_extract.JSONExtractError as exc:
            return _Outcome(kind="failure", error=f"JSONExtractError: {exc}")

        if not isinstance(payload, dict):
            type_name = type(payload).__name__
            return _Outcome(
                kind="failure",
                error=f"CitationValidationError: expected object, got {type_name}",
            )

        try:
            validated = citations.validate(payload)
        except citations.CitationValidationError as exc:
            return _Outcome(kind="failure", error=f"CitationValidationError: {exc}")

        return _Outcome(kind="success", validated=validated)
    finally:
        if proc is not None:
            with contextlib.suppress(Exception):
                subprocess_runner.release(proc)


async def _terminate_proc(proc: asyncio.subprocess.Process) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(pgid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=_SIGTERM_GRACE_S)
        return
    except TimeoutError:
        pass
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(pgid, signal.SIGKILL)


def _decode(data: bytes | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


def _pick_different(
    router: QuotaRouter, tier: Tier, *, exclude: ModelSpec
) -> ModelSpec:
    first = router.pick_model(tier)
    if first.to_id() != exclude.to_id():
        return first
    # WHY mark-then-pick: the router lacks an "exclude" knob; transient
    # blacklist forces a different candidate. Phase 4 retry budget is small
    # enough that this side-effect is acceptable.
    router.mark_blacklisted(exclude.cli)
    second = router.pick_model(tier)
    return second


async def _finalize_success(
    *,
    job: ResearchJob,
    clock_fn: Callable[[], float],
    model_spec: ModelSpec,
    validated: citations.ValidatedAnswer,
    on_event: WorkerEventSink,
) -> ResearchJob:
    artifact = _write_artifact(
        job.session_dir, job.q_id, validated, model_spec, job.flag
    )
    job.artifact_path = artifact
    job.completed_at = clock_fn()
    if job.started_at is not None:
        job.duration_s = job.completed_at - job.started_at

    has_substantive_citation = any(
        c.source_type in ("web", "local") for c in validated.citations
    )
    if validated.unsourced_claims and not has_substantive_citation:
        job.error = "all-claims-unsourced"

    job.status = "done"

    try:
        state.set_question_status(
            job.session_dir,
            q_id=job.q_id,
            status="answered",
        )
    except Exception as exc:
        logger.warning("state.set_question_status failed for %s: %s", job.q_id, exc)

    await _emit(on_event, "job_succeeded", job)
    return job


async def _finalize_failure(
    job: ResearchJob,
    clock_fn: Callable[[], float],
    error: str,
    *,
    status: JobStatus = "failed",
    on_event: WorkerEventSink = None,
    event_type: str = "job_failed",
) -> ResearchJob:
    job.error = error
    job.status = status
    job.completed_at = clock_fn()
    if job.started_at is not None:
        job.duration_s = job.completed_at - job.started_at
    await _emit(on_event, event_type, job)
    return job


async def _emit(
    on_event: WorkerEventSink,
    event_type: str,
    job: ResearchJob,
    *,
    extra: dict | None = None,
) -> None:
    if on_event is None:
        return
    try:
        await on_event(WorkerEvent(type=event_type, job=job, extra=extra or {}))
    except Exception:
        logger.exception("worker on_event hook raised")


_429_PATTERNS = (
    re.compile(r"\b429\b"),
    re.compile(r"rate[_\s-]?limit", re.IGNORECASE),
    re.compile(r"quota\s+exceeded", re.IGNORECASE),
)


def _detect_429(stdout_text: str, stderr_text: str) -> bool:
    combined = f"{stdout_text}\n{stderr_text}"
    return any(p.search(combined) for p in _429_PATTERNS)


def _build_prompt(job: ResearchJob, model_spec: ModelSpec) -> str:
    return (
        f"Meeting context: {job.meeting_context}\n\n"
        f"Question: {job.flag.topic}\n"
        f"Rationale: {job.flag.rationale}\n\n"
        f"Triggering transcript window:\n{job.flag.transcript_window}\n\n"
        "Answer the question. Every claim must carry a citation:\n"
        "- web citations: provide url + quoted_snippet\n"
        "- local citations: provide file_path + quoted_snippet\n"
        "- unsourced: only when you cannot back the claim\n\n"
        "Return ONLY a JSON object of shape:\n"
        '{"answer": "<prose with [1] [2] refs>", '
        '"citations": [{"id": <int>, "source_type": "web|local|unsourced", '
        '"url": "...", "file_path": "...", "quoted_snippet": "..."}]}\n'
        f"Model picked: {model_spec.to_id()}\n"
    )


def _build_argv(model_spec: ModelSpec, prompt: str) -> list[str]:
    # WHY per-CLI argv shapes: each provider's CLI uses different verbs and
    # flags; there is no shared invocation form to normalize upstream.
    cli = model_spec.cli
    if cli == "claude":
        return ["claude", "-p", prompt, "--model", model_spec.model]
    if cli == "codex":
        argv = ["codex", "exec", prompt, "--model", model_spec.model]
        if model_spec.effort_flag:
            argv.extend(model_spec.effort_flag.split())
        return argv
    if cli == "gemini":
        return ["gemini", "-p", prompt, "--model", model_spec.model]
    if cli == "vibe":
        return ["vibe", "-p", prompt, "--model", model_spec.model]
    return [cli, "-p", prompt, "--model", model_spec.model]


def _write_artifact(
    session_dir: Path,
    q_id: str,
    validated: citations.ValidatedAnswer,
    model_spec: ModelSpec,
    flag: Flag,
) -> Path:
    research_dir = session_dir / "hydra" / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = research_dir / f"{q_id}.md"

    lines: list[str] = []
    lines.append(f"# {q_id.upper()}: {flag.topic}")
    lines.append("")
    ts = datetime.now(UTC).isoformat()
    lines.append(f"**Flagged:** {ts} (auto, confidence {flag.confidence:.2f})")
    lines.append(f'**Triggering context:** "{flag.transcript_window}"')
    lines.append("**Status:** Refined (deep research complete)")
    lines.append(f"**Model:** {model_spec.to_id()}")
    lines.append("")
    lines.append("## Answer")
    lines.append(validated.answer)
    lines.append("")
    lines.append("## Citations")
    if validated.citations:
        for c in validated.citations:
            if c.source_type == "web":
                lines.append(f'{c.id}. **[Web]** {c.url} — "{c.quoted_snippet}"')
            elif c.source_type == "local":
                lines.append(
                    f'{c.id}. **[Local]** `{c.file_path}` — "{c.quoted_snippet}"'
                )
            else:
                lines.append(f'{c.id}. **[Unsourced]** "{c.quoted_snippet}"')
    else:
        lines.append("_(no citations)_")
    lines.append("")
    if validated.unsourced_claims:
        lines.append("## Unsourced (model assertion only)")
        for claim in validated.unsourced_claims:
            lines.append(f"- {claim}")
        lines.append("")

    artifact_path.write_text("\n".join(lines), encoding="utf-8")
    return artifact_path
