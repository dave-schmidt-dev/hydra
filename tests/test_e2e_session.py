"""End-to-end Hydra pipeline test with mocked LLM CLIs.

Drives the full stack: tailer reads ``tests/fixtures/e2e_session/transcript.jsonl``
into the watcher (with a mocked model returning canned flag responses), the
dispatcher pulls flags into the worker queue, mocked workers write q-NNN.md
artifacts, the report writer synthesizes report.md.

Verifies: expected questions flagged, expected artifacts written, expected
report content, no recording-integrity violations, watcher fallback NOT
triggered, dispatcher stats consistent.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hydra import audit, report, state
from hydra.dispatcher import Dispatcher, DispatcherConfig
from hydra.models import ModelSpec
from hydra.quota import NoCandidateModelError, QuotaRouter
from hydra.tailer import TranscriptTailer
from hydra.watcher import Banner, Flag, Watcher

FIXTURE = Path(__file__).parent / "fixtures" / "e2e_session"

PRIMARY_WATCHER = ModelSpec(cli="claude", model="claude-haiku-4-5", hard_timeout_s=10.0)
FALLBACK_WATCHER = ModelSpec(cli="codex", model="gpt-5.4-mini", hard_timeout_s=10.0)
FAST = ModelSpec(cli="claude", model="claude-haiku-4-5", hard_timeout_s=60.0)
HEAVY = ModelSpec(cli="claude", model="claude-opus-4-7", hard_timeout_s=300.0)

# Canned watcher rules: (substring-in-prompt, returned-flag-dict)
WATCHER_RULES: list[tuple[str, dict]] = [
    (
        "sla commitment",
        {
            "topic": "SLA commitment to enterprise customers",
            "confidence": 0.85,
            "rationale": "speaker explicitly asked what was promised in the contract",
        },
    ),
    (
        "retention policy for audio recordings",
        {
            "topic": "Retention policy for audio recordings",
            "confidence": 0.8,
            "rationale": "open decision flagged before compliance review",
        },
    ),
    (
        "confirmed the deployment date",
        {
            "topic": "Deployment date confirmation with customer",
            "confidence": 0.75,
            "rationale": "concrete go-live date is unverified",
        },
    ),
    (
        "fts5 over whoosh",
        {
            "topic": "Adopt FTS5 over Whoosh for the indexer",
            "confidence": 0.72,
            "rationale": "technical migration proposal pending benchmark",
        },
    ),
]


class CannedWatcherModel:
    """Substring-matching async invoker that returns canned flag candidates.

    Mirrors the real ``ModelInvoker`` contract: ``async (prompt, spec) -> list[dict]``.
    Each rule may fire at most once per instance so repeat ticks don't re-flag.
    """

    def __init__(self, rules: list[tuple[str, dict]]) -> None:
        self._rules = rules
        self._fired: set[int] = set()
        self.calls = 0
        self.seen_models: list[ModelSpec] = []

    async def __call__(self, prompt: str, model_spec: ModelSpec) -> list[dict]:
        self.calls += 1
        self.seen_models.append(model_spec)
        prompt_l = prompt.lower()
        # Return at most one candidate per call. Returning all matches at once
        # would let the watcher's intra-tick dedup (character-Jaccard >= 0.5
        # over recent topics) collapse our distinct English topics into one.
        # One-per-tick + dedup_window_seconds=0.0 sidesteps that entirely.
        for idx, (substr, flag) in enumerate(self._rules):
            if idx in self._fired:
                continue
            if substr.lower() in prompt_l:
                self._fired.add(idx)
                return [flag]
        return []


def _canned_worker_response(q_id: str, topic: str) -> str:
    """Build a Section 5.5-shaped JSON payload parameterised by topic."""
    return json.dumps(
        {
            "answer": (
                f"Research for {q_id} on '{topic}': the relevant policy lives in the "
                f"master agreement [1]. Internal notes on this topic exist in the "
                f"repository [2]."
            ),
            "citations": [
                {
                    "id": 1,
                    "source_type": "web",
                    "url": "https://example.invalid/master-agreement",
                    "quoted_snippet": (
                        f"Reference snippet for topic: {topic}. "
                        "See master agreement section 4.2."
                    ),
                },
                {
                    "id": 2,
                    "source_type": "local",
                    "file_path": "docs/policies.md",
                    "quoted_snippet": (
                        f"Internal note covering {topic}: see policies.md."
                    ),
                },
            ],
        }
    )


_REPORT_MD = (
    "## Meeting summary\n\n"
    "The team reviewed reliability commitments, audio-recording retention, the "
    "imminent customer deployment, and a proposed indexer migration. Several "
    "items remained open at the end of the call, with action owners assigned.\n\n"
    "## Questions raised\n\n"
    "- Q-001 (SLA commitment to enterprise customers): the master agreement "
    "section 4.2 governs the answer; see citation [1].\n"
    "- Q-002 (Retention policy for audio recordings): legal recommends a 90-day "
    "minimum pending the compliance memo.\n"
    "- Q-003 (Deployment date confirmation with customer): a June 1st soft "
    "target exists; written confirmation is still outstanding.\n"
    "- Q-004 (Adopt FTS5 over Whoosh): pending a benchmark on a representative "
    "corpus.\n\n"
    "## Findings\n\n"
    "- Multiple commitments live in documents the meeting participants did not "
    "have to hand.\n"
    "- Several decisions are blocked on confirmations from people outside the "
    "room.\n"
    "- A technical migration has consensus support but no benchmark data yet.\n\n"
    "## Suggested follow-ups\n\n"
    "- Priya: locate and circulate the master agreement excerpt covering SLAs.\n"
    "- Legal: finalise the audio-retention memo.\n"
    "- Marcus: obtain written deployment-date confirmation.\n"
    "- Sam: draft the FTS5 vs Whoosh benchmark plan.\n"
)


class _FakeProc:
    """Mimics ``asyncio.subprocess.Process`` for worker/report spawn stubs."""

    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0):
        self._stdout = stdout.encode()
        self._stderr = stderr.encode()
        self.returncode = returncode
        self.pid = 99999
        self.communicate = AsyncMock(side_effect=self._communicate_impl)

    async def _communicate_impl(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def _make_worker_spawn() -> Callable[..., Awaitable[_FakeProc]]:
    """Build a spawn stub that parses the worker prompt and returns a canned reply.

    The worker prompt embeds the question topic in a ``Question: <topic>`` line
    (see ``hydra.worker._build_prompt``); we extract it to parameterise the
    response so each artifact carries the right topic in its citations text.
    """

    async def spawn(argv, *, label, **kwargs):
        # Worker argv is a single big prompt string; for claude it's argv[2].
        prompt = ""
        for i, tok in enumerate(argv):
            if tok == "-p" and i + 1 < len(argv):
                prompt = argv[i + 1]
                break
        topic = "unknown"
        q_id = "q-???"
        for line in prompt.splitlines():
            if line.startswith("Question: "):
                topic = line[len("Question: ") :].strip()
            elif line.startswith("Model picked:"):
                pass
        # The label format is "worker:<q_id>:<model_id>".
        if label.startswith("worker:"):
            parts = label.split(":", 2)
            if len(parts) >= 2:
                q_id = parts[1]
        return _FakeProc(stdout=_canned_worker_response(q_id, topic), returncode=0)

    return spawn


def _make_report_spawn() -> Callable[..., Awaitable[_FakeProc]]:
    async def spawn(argv, *, label, **kwargs):
        return _FakeProc(stdout=_REPORT_MD, returncode=0)

    return spawn


def _make_router() -> QuotaRouter:
    """Round-robin router pinned to claude:* across all tiers (no real subprocess)."""
    tiers = {
        "watcher": [PRIMARY_WATCHER, FALLBACK_WATCHER],
        "fast": [FAST],
        "heavy": [HEAVY],
    }
    return QuotaRouter(
        tiers=tiers,
        cli_available={"claude", "codex"},
        fetch=lambda: None,
    )


async def _write_transcript_progressively(
    src: Path, dest: Path, *, lines_per_chunk: int = 5, delay_s: float = 0.02
) -> None:
    """Append the fixture transcript to ``dest`` in chunks so the tailer follows it.

    The tailer's run loop polls/waits for file changes; a single one-shot copy
    would deliver every event in one drain pass before the watcher's first
    tick fires. Chunked appends keep the watcher's rolling window populated
    across multiple ticks.
    """
    lines = src.read_text(encoding="utf-8").splitlines(keepends=True)
    with dest.open("w", encoding="utf-8") as fh:
        fh.write("")
    for i in range(0, len(lines), lines_per_chunk):
        chunk = "".join(lines[i : i + lines_per_chunk])
        with dest.open("a", encoding="utf-8") as fh:
            fh.write(chunk)
            fh.flush()
        await asyncio.sleep(delay_s)


@pytest.mark.e2e
async def test_e2e_pipeline_produces_artifacts_and_report(tmp_path: Path) -> None:
    state._reset_breaker_for_tests()

    # ---- 1. Stage the fixture under tmp_path (recording-integrity-safe). ----
    session_dir = tmp_path / "e2e_session"
    session_dir.mkdir()
    (session_dir / "audio.wav").write_bytes((FIXTURE / "audio.wav").read_bytes())
    transcript_path = session_dir / "transcript.jsonl"
    # The tailer expects the file to exist at startup but it can be empty;
    # _write_transcript_progressively will append to it.
    transcript_path.write_text("", encoding="utf-8")

    state.init_session_db(session_dir)
    ctx = {
        "meeting_about": "Platform sync: reliability, retention, launch",
        "participants": ["Alice", "Priya", "Marcus", "Sam"],
        "corpus_paths": [],
        "obsidian_export_dir": None,
        "hydra_started_at": "2026-05-13T10:00:00+00:00",
    }
    (session_dir / "hydra").mkdir(parents=True, exist_ok=True)
    (session_dir / "hydra" / "meeting_context.json").write_text(
        json.dumps(ctx, indent=2), encoding="utf-8"
    )

    # ---- 2. Wire the real components with mocked LLM seams. ----
    canned_watcher = CannedWatcherModel(WATCHER_RULES)
    flags_emitted: list[Flag] = []
    banners_emitted: list[Banner] = []
    router = _make_router()

    # Dispatcher needs to receive each flag the watcher emits.
    dispatcher: Dispatcher | None = None

    async def on_flag(flag: Flag) -> None:
        flags_emitted.append(flag)
        # Forward to the dispatcher once it is constructed (it is below).
        assert dispatcher is not None
        await dispatcher.enqueue_flag(flag)

    async def on_banner(banner: Banner) -> None:
        banners_emitted.append(banner)

    q_id_counter = itertools.count(1)

    def next_q_id() -> str:
        return f"q-{next(q_id_counter):03d}"

    tailer = TranscriptTailer(
        transcript_path,
        queue_maxsize=1_000,
        session_dir=session_dir,
        poll_interval_s=0.02,
    )

    watcher = Watcher(
        event_queue=tailer.queue,
        model_invoker=canned_watcher,
        primary_model=PRIMARY_WATCHER,
        fallback_model=FALLBACK_WATCHER,
        meeting_context=ctx["meeting_about"],
        tick_seconds=0.08,
        window_seconds=180.0,
        dedup_window_seconds=0.0,
        on_flag=on_flag,
        on_banner=on_banner,
        next_q_id=next_q_id,
    )

    dispatcher = Dispatcher(
        quota_router=router,
        session_dir=session_dir,
        meeting_context=ctx["meeting_about"],
        config=DispatcherConfig(quick_concurrency=2, deep_concurrency=2),
        spawn=_make_worker_spawn(),
    )

    # ---- 3. Run the pipeline under a strict total deadline. ----
    async def pipeline() -> None:
        tailer_task = asyncio.create_task(tailer.run(), name="tailer")
        watcher_task = asyncio.create_task(watcher.run(), name="watcher")
        dispatcher_task = asyncio.create_task(dispatcher.run(), name="dispatcher")
        feeder_task = asyncio.create_task(
            _write_transcript_progressively(
                FIXTURE / "transcript.jsonl", transcript_path
            ),
            name="feeder",
        )

        try:
            # Wait until the feeder has delivered the whole transcript.
            await feeder_task
            # Phase A: let the watcher tick enough times to drain its rules.
            # With tick_seconds=0.08 and 4 rules to fire, ~1.0s of wall time
            # gives ~12 ticks — more than enough headroom but well under the
            # outer 8s budget.
            watcher_idle_deadline = asyncio.get_running_loop().time() + 1.5
            last_flag_count = -1
            stable_ticks = 0
            while asyncio.get_running_loop().time() < watcher_idle_deadline:
                if len(flags_emitted) == last_flag_count:
                    stable_ticks += 1
                    # Quiescent: no new flag in 3 consecutive polls (~150ms).
                    if stable_ticks >= 3 and len(flags_emitted) >= 4:
                        break
                else:
                    stable_ticks = 0
                    last_flag_count = len(flags_emitted)
                await asyncio.sleep(0.05)

            # Phase B: drain the dispatcher's queues fully.
            drain_deadline = asyncio.get_running_loop().time() + 3.0
            while asyncio.get_running_loop().time() < drain_deadline:
                stats = dispatcher.stats
                if (
                    stats["quick_pending"] == 0
                    and stats["deep_pending"] == 0
                    and stats["quick_active"] == 0
                    and stats["deep_active"] == 0
                    and (stats["quick_completed"] + stats["quick_failed"])
                    >= len(flags_emitted)
                    and (stats["deep_completed"] + stats["deep_failed"])
                    >= len(flags_emitted)
                ):
                    break
                await asyncio.sleep(0.02)
        finally:
            watcher.stop()
            dispatcher.stop()
            tailer.stop()
            for t in (watcher_task, dispatcher_task, tailer_task):
                try:
                    await asyncio.wait_for(t, timeout=2.0)
                except (TimeoutError, asyncio.CancelledError):
                    t.cancel()

    await asyncio.wait_for(pipeline(), timeout=8.0)

    # ---- 4. Run the report writer (also mocked). ----
    report_result = await asyncio.wait_for(
        report.generate_report(
            session_dir,
            quota_router=router,
            spawn=_make_report_spawn(),
        ),
        timeout=2.0,
    )

    # ---- 5. Assertions. ----

    # 5a. At least 3 flags fired (we ship 4 flag-worthy rules).
    assert len(flags_emitted) >= 3, (
        f"expected >=3 watcher flags, got {len(flags_emitted)}: "
        f"{[f.topic for f in flags_emitted]}"
    )

    # 5b. questions.jsonl contains flagged + answered events.
    audit_events = audit.tail(session_dir)
    flagged_q_ids = {e["q_id"] for e in audit_events if e.get("event") == "flagged"}
    answered_q_ids = {e["q_id"] for e in audit_events if e.get("event") == "answered"}
    assert len(flagged_q_ids) >= 3
    assert flagged_q_ids.issubset(answered_q_ids), (
        f"some flagged questions never reached answered: "
        f"flagged={flagged_q_ids} answered={answered_q_ids}"
    )

    # 5c. Per-question artifacts exist under <session>/hydra/research/.
    research_dir = session_dir / "hydra" / "research"
    artifact_paths = sorted(research_dir.glob("q-*.md"))
    assert len(artifact_paths) >= len(flagged_q_ids), (
        f"expected one artifact per flagged question, got "
        f"{len(artifact_paths)} for {len(flagged_q_ids)} flags"
    )
    for art in artifact_paths:
        body = art.read_text(encoding="utf-8")
        q_upper = art.stem.upper()
        assert body.startswith(f"# {q_upper}:"), (
            f"{art.name} missing '# {q_upper}:' header; got: {body[:80]!r}"
        )
        assert "## Answer" in body
        assert "## Citations" in body
        assert "example.invalid/master-agreement" in body

    # 5d. state.db questions table has at least 3 answered rows.
    conn = state.open_session_db(session_dir)
    try:
        rows = conn.execute("SELECT q_id, status FROM questions ORDER BY id").fetchall()
    finally:
        conn.close()
    answered_rows = [r for r in rows if r["status"] == "answered"]
    assert len(answered_rows) >= 3, (
        f"expected >=3 answered rows in state.db, got {len(answered_rows)}: "
        f"{[(r['q_id'], r['status']) for r in rows]}"
    )

    # 5e. Report writer outputs both files with the four expected sections.
    assert report_result.error is None, f"report error: {report_result.error}"
    assert report_result.report_path.exists()
    assert report_result.raw_output_path.exists()
    report_body = report_result.report_path.read_text(encoding="utf-8")
    for section in (
        "## Meeting summary",
        "## Questions raised",
        "## Findings",
        "## Suggested follow-ups",
    ):
        assert section in report_body, f"report missing section: {section}"
    raw_body = report_result.raw_output_path.read_text(encoding="utf-8")
    assert "Meeting summary" in raw_body

    # 5f. Watcher did NOT flip to fallback — canned invoker never raises.
    assert watcher.on_fallback is False, (
        "watcher unexpectedly switched to fallback model"
    )
    assert watcher.current_model == PRIMARY_WATCHER
    # No banner whose severity is error (warning banners would imply failures).
    assert not any(b.severity == "error" for b in banners_emitted), (
        f"unexpected error banners: {banners_emitted}"
    )

    # 5g. Dispatcher stats are internally consistent.
    final_stats = dispatcher.stats
    assert final_stats["quick_pending"] == 0
    assert final_stats["deep_pending"] == 0
    assert final_stats["quick_active"] == 0
    assert final_stats["deep_active"] == 0
    assert final_stats["quick_completed"] >= len(flagged_q_ids)
    assert final_stats["deep_completed"] >= len(flagged_q_ids)
    assert final_stats["quick_failed"] == 0
    assert final_stats["deep_failed"] == 0

    # 5h. The autouse recording-integrity fixture would have raised by now
    # if production code wrote outside the allowlist; reaching this line
    # means every artifact, audit line, and report write stayed inside
    # tmp_path or the cache/log sentinels.


@pytest.mark.e2e
def test_canned_router_never_raises_for_configured_tiers() -> None:
    """Sanity check the test scaffolding itself."""
    router = _make_router()
    fast = router.pick_model("fast")
    heavy = router.pick_model("heavy")
    watcher = router.pick_model("watcher")
    assert fast.cli == "claude"
    assert heavy.cli == "claude"
    assert watcher.cli in {"claude", "codex"}
    # Sanity: ensure NoCandidateModelError is reachable in the test module.
    bad = QuotaRouter(cli_available=set(), fetch=lambda: None)
    with pytest.raises(NoCandidateModelError):
        bad.pick_model("fast")
