"""Tests for hydra.worker — single research-job lifecycle (Phase 4 Task 4.2)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hydra import state, subprocess_runner, worker
from hydra.models import ModelSpec
from hydra.quota import NoCandidateModelError, QuotaRouter
from hydra.watcher import Flag

FAST_PRIMARY = ModelSpec(cli="claude", model="claude-haiku-4-5", hard_timeout_s=60.0)
FAST_FALLBACK_A = ModelSpec(cli="codex", model="gpt-5.4-mini", hard_timeout_s=60.0)
FAST_FALLBACK_B = ModelSpec(cli="gemini", model="gemini-2.5-flash", hard_timeout_s=60.0)

HEAVY_PRIMARY = ModelSpec(cli="claude", model="claude-opus-4-7", hard_timeout_s=300.0)
HEAVY_FALLBACK_A = ModelSpec(
    cli="codex",
    model="gpt-5.5",
    effort_flag="--effort high",
    hard_timeout_s=300.0,
)
HEAVY_FALLBACK_B = ModelSpec(cli="gemini", model="gemini-2.5-pro", hard_timeout_s=300.0)


def _valid_response_json() -> str:
    return json.dumps(
        {
            "answer": "Capital of France is Paris [1].",
            "citations": [
                {
                    "id": 1,
                    "source_type": "web",
                    "url": "https://example.com/paris",
                    "quoted_snippet": "Paris is the capital of France.",
                }
            ],
        }
    )


def _make_flag(q_id: str = "q-001", topic: str = "Capital of France") -> Flag:
    return Flag(
        q_id=q_id,
        topic=topic,
        rationale="user asked",
        confidence=0.85,
        transcript_window="speaker: what's the capital of france",
        status="pending",
    )


def _make_job(
    session_dir: Path,
    *,
    tier: str = "fast",
    q_id: str = "q-001",
) -> worker.ResearchJob:
    return worker.ResearchJob(
        q_id=q_id,
        tier=tier,
        flag=_make_flag(q_id=q_id),
        session_dir=session_dir,
        meeting_context="meeting about geography",
    )


class _FakeProc:
    """Mimics asyncio.subprocess.Process with scripted communicate behavior."""

    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        timeout: bool = False,
        pid: int = 9999,
    ) -> None:
        self._stdout = stdout.encode()
        self._stderr = stderr.encode()
        self.returncode = returncode
        self._timeout = timeout
        self.pid = pid
        self.communicate = AsyncMock(side_effect=self._communicate_impl)

    async def _communicate_impl(self) -> tuple[bytes, bytes]:
        if self._timeout:
            raise TimeoutError("scripted timeout")
        return self._stdout, self._stderr


def _make_spawn_stub(
    procs: list[_FakeProc],
    *,
    record: list[list[str]] | None = None,
):
    """Return an async spawn callable that pops a pre-built proc per call."""
    iterator = iter(procs)

    async def spawn(argv, *, label, **kwargs):
        if record is not None:
            record.append(list(argv))
        try:
            return next(iterator)
        except StopIteration as exc:
            raise AssertionError("spawn called more times than scripted") from exc

    return spawn


def _make_router(
    *,
    tiers: dict | None = None,
    pick_sequence: list[ModelSpec] | None = None,
) -> QuotaRouter:
    if tiers is None:
        tiers = {
            "fast": [FAST_PRIMARY, FAST_FALLBACK_A, FAST_FALLBACK_B],
            "heavy": [HEAVY_PRIMARY, HEAVY_FALLBACK_A, HEAVY_FALLBACK_B],
            "watcher": [FAST_PRIMARY, FAST_FALLBACK_A],
        }
    router = QuotaRouter(
        tiers=tiers,
        cli_available={"claude", "codex", "gemini", "vibe"},
        fetch=lambda: None,
    )
    if pick_sequence is not None:
        seq = iter(pick_sequence)

        def picker(tier):
            try:
                return next(seq)
            except StopIteration:
                raise NoCandidateModelError(
                    f"scripted pick_sequence exhausted for tier {tier!r}"
                ) from None

        router.pick_model = picker  # type: ignore[method-assign]
    return router


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    state._reset_breaker_for_tests()
    state.init_session_db(tmp_path)
    state.insert_question(
        tmp_path,
        q_id="q-001",
        status="pending",
        source="heuristic",
        topic="Capital of France",
    )
    (tmp_path / "hydra" / "research").mkdir(parents=True, exist_ok=True)
    return tmp_path


class TestHappyPath:
    async def test_subprocess_success_writes_artifact_and_status(
        self, session_dir: Path
    ) -> None:
        events: list[worker.WorkerEvent] = []

        async def on_event(ev: worker.WorkerEvent) -> None:
            events.append(ev)

        proc = _FakeProc(stdout=_valid_response_json(), returncode=0)
        spawn = _make_spawn_stub([proc])
        router = _make_router(pick_sequence=[FAST_PRIMARY])

        job = _make_job(session_dir, tier="fast")
        result = await worker.run_research_job(
            job,
            quota_router=router,
            on_event=on_event,
            spawn=spawn,
        )

        assert result.status == "done"
        assert result.model_spec == FAST_PRIMARY
        assert result.artifact_path is not None
        assert result.artifact_path.exists()
        content = result.artifact_path.read_text()
        assert "Q-001" in content
        assert "Paris" in content
        assert "https://example.com/paris" in content
        assert "claude:claude-haiku-4-5" in content

        kinds = [e.type for e in events]
        assert "job_started" in kinds
        assert "job_succeeded" in kinds


class TestSubprocessFailure:
    async def test_returncode_nonzero_marks_failed(self, session_dir: Path) -> None:
        events: list[worker.WorkerEvent] = []

        async def on_event(ev: worker.WorkerEvent) -> None:
            events.append(ev)

        proc = _FakeProc(stdout="", stderr="oh no", returncode=1)
        spawn = _make_spawn_stub([proc])
        router = _make_router(pick_sequence=[FAST_PRIMARY])

        job = _make_job(session_dir, tier="fast")
        result = await worker.run_research_job(
            job,
            quota_router=router,
            on_event=on_event,
            spawn=spawn,
        )

        assert result.status == "failed"
        assert "exit 1" in (result.error or "")
        assert "oh no" in (result.error or "")
        assert any(e.type == "job_failed" for e in events)


class TestTimeout:
    async def test_quick_tier_timeout(self, session_dir: Path) -> None:
        events: list[worker.WorkerEvent] = []

        async def on_event(ev: worker.WorkerEvent) -> None:
            events.append(ev)

        proc = _FakeProc(timeout=True)
        spawn = _make_spawn_stub([proc])
        router = _make_router(pick_sequence=[FAST_PRIMARY])

        job = _make_job(session_dir, tier="fast")
        result = await worker.run_research_job(
            job,
            quota_router=router,
            on_event=on_event,
            spawn=spawn,
        )

        assert result.status == "timeout"
        assert result.auto_retried is False
        assert any(e.type == "job_timeout" for e in events)

    async def test_heavy_tier_timeout_then_retry_succeeds(
        self, session_dir: Path
    ) -> None:
        events: list[worker.WorkerEvent] = []

        async def on_event(ev: worker.WorkerEvent) -> None:
            events.append(ev)

        timeout_proc = _FakeProc(timeout=True)
        success_proc = _FakeProc(stdout=_valid_response_json(), returncode=0)
        spawn = _make_spawn_stub([timeout_proc, success_proc])
        router = _make_router(pick_sequence=[HEAVY_PRIMARY, HEAVY_FALLBACK_A])

        job = _make_job(session_dir, tier="heavy")
        result = await worker.run_research_job(
            job,
            quota_router=router,
            on_event=on_event,
            spawn=spawn,
        )

        assert result.status == "done"
        assert result.auto_retried is True
        assert result.model_spec == HEAVY_FALLBACK_A
        assert result.model_spec != HEAVY_PRIMARY

    async def test_heavy_tier_double_timeout(self, session_dir: Path) -> None:
        timeout_proc_a = _FakeProc(timeout=True)
        timeout_proc_b = _FakeProc(timeout=True)
        spawn = _make_spawn_stub([timeout_proc_a, timeout_proc_b])
        router = _make_router(pick_sequence=[HEAVY_PRIMARY, HEAVY_FALLBACK_A])

        job = _make_job(session_dir, tier="heavy")
        result = await worker.run_research_job(
            job,
            quota_router=router,
            spawn=spawn,
        )

        assert result.status == "timeout"
        assert result.auto_retried is True
        assert "auto-retry exhausted" in (result.error or "")


class TestMalformedOutput:
    async def test_malformed_json(self, session_dir: Path) -> None:
        proc = _FakeProc(stdout="just some prose without JSON", returncode=0)
        spawn = _make_spawn_stub([proc])
        router = _make_router(pick_sequence=[FAST_PRIMARY])

        job = _make_job(session_dir, tier="fast")
        result = await worker.run_research_job(
            job,
            quota_router=router,
            spawn=spawn,
        )

        assert result.status == "failed"
        assert "JSONExtractError" in (result.error or "")

    async def test_validation_failure(self, session_dir: Path) -> None:
        bad = json.dumps({"answer": 42, "citations": []})
        proc = _FakeProc(stdout=bad, returncode=0)
        spawn = _make_spawn_stub([proc])
        router = _make_router(pick_sequence=[FAST_PRIMARY])

        job = _make_job(session_dir, tier="fast")
        result = await worker.run_research_job(
            job,
            quota_router=router,
            spawn=spawn,
        )

        assert result.status == "failed"
        assert "CitationValidationError" in (result.error or "")


class TestAllUnsourced:
    async def test_all_claims_unsourced_writes_artifact_marks_soft_flag(
        self, session_dir: Path
    ) -> None:
        payload = json.dumps(
            {
                "answer": "I think the capital is Paris [1].",
                "citations": [
                    {
                        "id": 1,
                        "source_type": "unsourced",
                        "quoted_snippet": "The capital is Paris.",
                    }
                ],
            }
        )
        proc = _FakeProc(stdout=payload, returncode=0)
        spawn = _make_spawn_stub([proc])
        router = _make_router(pick_sequence=[FAST_PRIMARY])

        job = _make_job(session_dir, tier="fast")
        result = await worker.run_research_job(
            job,
            quota_router=router,
            spawn=spawn,
        )

        assert result.status == "done"
        assert result.error == "all-claims-unsourced"
        assert result.artifact_path is not None
        assert result.artifact_path.exists()
        content = result.artifact_path.read_text()
        assert "Unsourced" in content


class TestMidFlight429:
    async def test_429_detected_marks_blacklisted_and_reroutes(
        self, session_dir: Path
    ) -> None:
        first = _FakeProc(stdout="", stderr="rate_limit exceeded", returncode=1)
        second = _FakeProc(stdout=_valid_response_json(), returncode=0)
        spawn = _make_spawn_stub([first, second])
        router = _make_router(pick_sequence=[FAST_PRIMARY, FAST_FALLBACK_A])

        job = _make_job(session_dir, tier="fast")
        result = await worker.run_research_job(
            job,
            quota_router=router,
            spawn=spawn,
        )

        assert result.status == "done"
        assert result.model_spec == FAST_FALLBACK_A
        assert router.is_blacklisted("claude")

    async def test_three_429s_exhaust_tier(self, session_dir: Path) -> None:
        procs = [
            _FakeProc(stdout="", stderr="429 Too Many Requests", returncode=1),
            _FakeProc(stdout="", stderr="rate_limit exceeded", returncode=1),
            _FakeProc(stdout="", stderr="quota exceeded", returncode=1),
        ]
        spawn = _make_spawn_stub(procs)
        router = _make_router(
            pick_sequence=[FAST_PRIMARY, FAST_FALLBACK_A, FAST_FALLBACK_B]
        )

        job = _make_job(session_dir, tier="fast")
        result = await worker.run_research_job(
            job,
            quota_router=router,
            spawn=spawn,
        )

        assert result.status == "failed"
        assert "tier exhausted" in (result.error or "").lower()


class TestQuotaRouterErrors:
    async def test_no_candidate_propagates(self, session_dir: Path) -> None:
        router = _make_router(pick_sequence=[])  # raises on first pick

        async def unused_spawn(*args, **kwargs):
            raise AssertionError("spawn must not be invoked when no candidate")

        job = _make_job(session_dir, tier="fast")
        result = await worker.run_research_job(
            job,
            quota_router=router,
            spawn=unused_spawn,
        )

        assert result.status == "failed"
        assert "no candidate model" in (result.error or "").lower()


class TestRegistryCleanup:
    async def test_release_called_after_success(self, session_dir: Path) -> None:
        released: list[object] = []
        proc = _FakeProc(stdout=_valid_response_json(), returncode=0)
        spawn = _make_spawn_stub([proc])
        router = _make_router(pick_sequence=[FAST_PRIMARY])

        original_release = subprocess_runner.release

        def tracking_release(p) -> None:
            released.append(p)

        try:
            subprocess_runner.release = tracking_release  # type: ignore[assignment]
            job = _make_job(session_dir, tier="fast")
            result = await worker.run_research_job(
                job,
                quota_router=router,
                spawn=spawn,
            )
        finally:
            subprocess_runner.release = original_release  # type: ignore[assignment]

        assert result.status == "done"
        assert proc in released

    async def test_release_called_after_failure(self, session_dir: Path) -> None:
        released: list[object] = []
        proc = _FakeProc(stdout="", stderr="oh no", returncode=1)
        spawn = _make_spawn_stub([proc])
        router = _make_router(pick_sequence=[FAST_PRIMARY])

        original_release = subprocess_runner.release
        try:
            subprocess_runner.release = lambda p: released.append(p)  # type: ignore[assignment]
            job = _make_job(session_dir, tier="fast")
            result = await worker.run_research_job(
                job,
                quota_router=router,
                spawn=spawn,
            )
        finally:
            subprocess_runner.release = original_release  # type: ignore[assignment]

        assert result.status == "failed"
        assert proc in released


@pytest.mark.parametrize(
    "spec, expected_head",
    [
        (
            ModelSpec(cli="claude", model="claude-haiku-4-5"),
            ["claude", "-p", "<PROMPT>", "--model", "claude-haiku-4-5"],
        ),
        (
            ModelSpec(cli="codex", model="gpt-5.5", effort_flag="--effort high"),
            [
                "codex",
                "exec",
                "<PROMPT>",
                "--model",
                "gpt-5.5",
                "--effort",
                "high",
            ],
        ),
        (
            ModelSpec(cli="gemini", model="gemini-2.5-flash"),
            ["gemini", "-p", "<PROMPT>", "--model", "gemini-2.5-flash"],
        ),
        (
            ModelSpec(cli="vibe", model="mistral-codestral"),
            ["vibe", "-p", "<PROMPT>", "--model", "mistral-codestral"],
        ),
    ],
)
def test_build_argv_per_cli(spec: ModelSpec, expected_head: list[str]) -> None:
    argv = worker._build_argv(spec, "<PROMPT>")
    assert argv == expected_head


def test_detect_429_variants() -> None:
    assert worker._detect_429("", "429 Too Many Requests")
    assert worker._detect_429("", "rate_limit exceeded")
    assert worker._detect_429("", "quota exceeded")
    assert worker._detect_429("model says quota exceeded", "")
    assert not worker._detect_429("", "ok all good")
    assert not worker._detect_429("answer text 4290 widgets", "")


def test_research_job_dataclass_defaults() -> None:
    flag = _make_flag()
    job = worker.ResearchJob(
        q_id="q-001",
        tier="fast",
        flag=flag,
        session_dir=Path("/tmp/nope"),
    )
    assert job.status == "queued"
    assert job.auto_retried is False
    assert job.model_spec is None
    assert job.error is None


@pytest.mark.usefixtures("session_dir")
def test_worker_event_dataclass_shape() -> None:
    flag = _make_flag()
    job = worker.ResearchJob(
        q_id="q-001", tier="fast", flag=flag, session_dir=Path(".")
    )
    ev = worker.WorkerEvent(type="job_started", job=job)
    assert ev.type == "job_started"
    assert ev.job is job
    assert ev.extra == {}


@pytest.mark.usefixtures("session_dir")
async def test_state_status_updated_on_success(session_dir: Path) -> None:
    proc = _FakeProc(stdout=_valid_response_json(), returncode=0)
    spawn = _make_spawn_stub([proc])
    router = _make_router(pick_sequence=[FAST_PRIMARY])

    job = _make_job(session_dir, tier="fast")
    await worker.run_research_job(job, quota_router=router, spawn=spawn)

    conn = state.open_session_db(session_dir)
    try:
        row = conn.execute(
            "SELECT status FROM questions WHERE q_id = ?", ("q-001",)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "answered"


@pytest.mark.usefixtures("session_dir")
async def test_argv_passed_to_spawn_matches_build_argv(session_dir: Path) -> None:
    record: list[list[str]] = []
    proc = _FakeProc(stdout=_valid_response_json(), returncode=0)
    spawn = _make_spawn_stub([proc], record=record)
    router = _make_router(pick_sequence=[FAST_PRIMARY])

    job = _make_job(session_dir, tier="fast")
    await worker.run_research_job(job, quota_router=router, spawn=spawn)

    assert len(record) == 1
    argv = record[0]
    assert argv[0] == "claude"
    assert "--model" in argv
    assert "claude-haiku-4-5" in argv
