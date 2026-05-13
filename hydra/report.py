"""Post-session report writer.

Plan Section 4.5.8: runs once on ``session_end`` (or manual ``/finalize``).
Reads every retained ``q-NNN.md`` + state + ``meeting_context.json``, asks a
heavy-tier model for a single Markdown synthesis, and persists
``report.md`` (post-processed) plus ``report.generated.md`` (raw LLM output).
Optionally copies the post-processed report into the user's Obsidian vault
with YAML frontmatter.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hydra import audit, state, subprocess_runner, worker
from hydra.models import ModelSpec
from hydra.quota import NoCandidateModelError, QuotaRouter

logger = logging.getLogger("hydra.report")

REPORT_FILENAME = "report.md"
RAW_OUTPUT_FILENAME = "report.generated.md"
SYNTHESIS_TIMEOUT_S = 60.0
_RETAINED_STATUSES = ("answered", "investigating")

SpawnFn = Callable[..., Awaitable]


@dataclass
class ReportResult:
    session_dir: Path
    report_path: Path
    raw_output_path: Path
    obsidian_copy_path: Path | None = None
    model_spec: ModelSpec | None = None
    duration_s: float = 0.0
    error: str | None = None
    questions_in_report: int = 0
    questions_pruned: int = 0


async def generate_report(
    session_dir: Path,
    *,
    quota_router: QuotaRouter,
    obsidian_export_dir: Path | None = None,
    spawn: SpawnFn | None = None,
    clock: Callable[[], float] | None = None,
) -> ReportResult:
    """Synthesize the post-session report. See module docstring for steps."""
    spawn_fn = spawn if spawn is not None else subprocess_runner.spawn
    clock_fn = clock if clock is not None else time.monotonic

    hydra_dir = session_dir / "hydra"
    hydra_dir.mkdir(parents=True, exist_ok=True)
    report_path = hydra_dir / REPORT_FILENAME
    raw_path = hydra_dir / RAW_OUTPUT_FILENAME

    started = clock_fn()
    meeting_context = _read_meeting_context(session_dir)
    retained, pruned_count = _read_retained_questions(session_dir)

    if not retained:
        return ReportResult(
            session_dir=session_dir,
            report_path=report_path,
            raw_output_path=raw_path,
            error="no questions to report",
            questions_in_report=0,
            questions_pruned=pruned_count,
            duration_s=clock_fn() - started,
        )

    try:
        model_spec = quota_router.pick_model("heavy")
    except NoCandidateModelError as exc:
        return ReportResult(
            session_dir=session_dir,
            report_path=report_path,
            raw_output_path=raw_path,
            error=f"no candidate model: {exc}",
            questions_in_report=len(retained),
            questions_pruned=pruned_count,
            duration_s=clock_fn() - started,
        )

    prompt = _build_synthesis_prompt(meeting_context, retained)
    argv = worker._build_argv(model_spec, prompt)
    label = f"report:{model_spec.to_id()}"

    raw_stdout = ""
    failure_error: str | None = None

    proc = None
    try:
        proc = await spawn_fn(argv, label=label)
    except TimeoutError:
        failure_error = "synthesis timeout"
    except Exception as exc:
        failure_error = f"spawn failed: {exc}"

    if proc is not None:
        try:
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=SYNTHESIS_TIMEOUT_S
                )
            except TimeoutError:
                failure_error = "synthesis timeout"
                stdout_b = b""
                stderr_b = b""
            raw_stdout = _decode(stdout_b)
            stderr_text = _decode(stderr_b)
            rc = proc.returncode
            if failure_error is None and rc not in (None, 0):
                tail = stderr_text[-500:] if stderr_text else raw_stdout[-500:]
                failure_error = f"exit {rc}: {tail}".strip()
            if failure_error is None and not raw_stdout.strip():
                failure_error = "empty synthesis output"
        finally:
            with contextlib.suppress(Exception):
                subprocess_runner.release(proc)

    raw_path.write_text(raw_stdout, encoding="utf-8")

    if failure_error is not None:
        placeholder = (
            f"# Report (synthesis failed)\n\n"
            f"Synthesis call to {model_spec.to_id()} failed: {failure_error}. "
            f"The raw output (if any) is preserved at "
            f"{RAW_OUTPUT_FILENAME}.\n"
        )
        report_path.write_text(placeholder, encoding="utf-8")
        duration = clock_fn() - started
        _emit_audit(
            session_dir,
            model_spec=model_spec,
            duration_s=duration,
            questions_in_report=len(retained),
            questions_pruned=pruned_count,
            error=failure_error,
        )
        return ReportResult(
            session_dir=session_dir,
            report_path=report_path,
            raw_output_path=raw_path,
            model_spec=model_spec,
            duration_s=duration,
            error=failure_error,
            questions_in_report=len(retained),
            questions_pruned=pruned_count,
        )

    processed = _postprocess_synthesis(raw_stdout)
    report_path.write_text(processed, encoding="utf-8")

    obsidian_copy: Path | None = None
    if obsidian_export_dir is not None:
        try:
            obsidian_copy = _write_obsidian_copy(
                report_path=report_path,
                obsidian_dir=obsidian_export_dir,
                meeting_context=meeting_context,
                model_id=model_spec.to_id(),
                question_count=len(retained),
                session_name=session_dir.name,
            )
        except Exception as exc:
            logger.warning("obsidian export failed: %s", exc)

    duration = clock_fn() - started
    finalized_at = datetime.now(UTC).isoformat()
    try:
        state.set_config(session_dir, "hydra.last_report_at", finalized_at)
    except Exception as exc:
        logger.warning("state.set_config last_report_at failed: %s", exc)

    _emit_audit(
        session_dir,
        model_spec=model_spec,
        duration_s=duration,
        questions_in_report=len(retained),
        questions_pruned=pruned_count,
        error=None,
    )

    return ReportResult(
        session_dir=session_dir,
        report_path=report_path,
        raw_output_path=raw_path,
        obsidian_copy_path=obsidian_copy,
        model_spec=model_spec,
        duration_s=duration,
        questions_in_report=len(retained),
        questions_pruned=pruned_count,
    )


def _decode(data: bytes | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


def _read_meeting_context(session_dir: Path) -> dict[str, Any]:
    path = session_dir / "hydra" / "meeting_context.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("meeting_context.json unreadable: %s", exc)
        return {}


def _read_retained_questions(
    session_dir: Path,
) -> tuple[list[dict[str, Any]], int]:
    db_path = session_dir / "hydra" / "state.db"
    if not db_path.exists():
        return [], 0

    conn = state.open_session_db(session_dir)
    try:
        rows = conn.execute(
            "SELECT q_id, topic, status, confidence, rationale, "
            "transcript_window, in_report FROM questions ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    research_dir = session_dir / "hydra" / "research"
    retained: list[dict[str, Any]] = []
    pruned = 0
    for row in rows:
        status = row["status"]
        in_report = row["in_report"]
        if status not in _RETAINED_STATUSES:
            if status == "dismissed" or in_report == 0:
                pruned += 1
            continue
        if in_report == 0:
            pruned += 1
            continue
        artifact_path = research_dir / f"{row['q_id']}.md"
        artifact_body: str | None = None
        if artifact_path.exists():
            try:
                artifact_body = artifact_path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("artifact %s unreadable: %s", artifact_path, exc)
                artifact_body = None
        retained.append(
            {
                "q_id": row["q_id"],
                "topic": row["topic"],
                "status": status,
                "confidence": row["confidence"],
                "rationale": row["rationale"],
                "transcript_window": row["transcript_window"],
                "artifact_path": artifact_path if artifact_body is not None else None,
                "artifact_body": artifact_body,
            }
        )
    return retained, pruned


def _build_synthesis_prompt(
    meeting_context: dict[str, Any], retained: list[dict[str, Any]]
) -> str:
    about = meeting_context.get("meeting_about", "") or ""
    participants_raw = meeting_context.get("participants") or []
    if isinstance(participants_raw, list):
        participants = ", ".join(str(p) for p in participants_raw)
    else:
        participants = str(participants_raw)
    started = meeting_context.get("hydra_started_at", "") or ""

    # WHY explicit sectioning: the synthesis model needs concrete section
    # headings or it tends to invent its own structure, breaking the
    # Obsidian export and downstream tooling.
    head = (
        f"Meeting context:\n"
        f"About: {about}\n"
        f"Participants: {participants}\n"
        f"Date: {started}\n\n"
        f"This meeting raised {len(retained)} questions. For each, you have "
        f"the existing research artifact below. Produce a single Markdown "
        f"report with these sections:\n\n"
        f"## Meeting summary\n"
        f"(2-3 paragraphs - what was discussed, what was decided, "
        f"what was uncertain.)\n\n"
        f"## Questions raised\n"
        f"For each question, one paragraph: the topic, the answer, and "
        f"the key citations.\n\n"
        f"## Findings\n"
        f"Synthesized cross-question insights (3-5 bullet points).\n\n"
        f"## Suggested follow-ups\n"
        f"Actionable items with possible owners (3-7 bullet points).\n\n"
        f"Preserve every citation that appears in the per-question "
        f"artifacts - do not strip URLs or file paths. Use the exact "
        f"citation snippets from the artifacts, do not paraphrase them.\n\n"
        f"===\n"
    )
    body_parts: list[str] = []
    for q in retained:
        body = q.get("artifact_body") or (
            f"# {q['q_id'].upper()}: {q['topic']}\n_(no artifact body available)_\n"
        )
        body_parts.append(body)
    return head + "\n===\n".join(body_parts) + "\n"


def _postprocess_synthesis(raw: str) -> str:
    text = raw.strip()
    # WHY unwrap fenced output: heavy-tier CLIs sometimes wrap the entire
    # response in a single ```markdown ... ``` block. Strip exactly one
    # outermost fence so the persisted report doesn't render the fences
    # literally.
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            inner = text[first_nl + 1 :]
            if inner.endswith("```"):
                inner = inner[:-3]
            elif inner.rstrip().endswith("```"):
                inner = inner.rstrip()[:-3]
            text = inner.strip()
    if not text.endswith("\n"):
        text = text + "\n"
    return text


def _write_obsidian_copy(
    *,
    report_path: Path,
    obsidian_dir: Path,
    meeting_context: dict[str, Any],
    model_id: str,
    question_count: int,
    session_name: str,
) -> Path:
    obsidian_dir.mkdir(parents=True, exist_ok=True)
    date_val = meeting_context.get("hydra_started_at") or datetime.now(UTC).isoformat()
    frontmatter = (
        f"---\n"
        f"date: {date_val}\n"
        f"session: {session_name}\n"
        f"model: {model_id}\n"
        f"questions: {question_count}\n"
        f"---\n\n"
    )
    body = report_path.read_text(encoding="utf-8")
    out = obsidian_dir / f"{session_name}-report.md"
    out.write_text(frontmatter + body, encoding="utf-8")
    return out


def _emit_audit(
    session_dir: Path,
    *,
    model_spec: ModelSpec,
    duration_s: float,
    questions_in_report: int,
    questions_pruned: int,
    error: str | None,
) -> None:
    payload: dict[str, Any] = {
        "event": "report_generated",
        "model": model_spec.to_id(),
        "duration_s": duration_s,
        "questions_in_report": questions_in_report,
        "questions_pruned": questions_pruned,
    }
    if error is not None:
        payload["error"] = error
    try:
        audit.emit(session_dir, payload)
    except Exception as exc:
        logger.warning("audit.emit failed for report_generated: %s", exc)
