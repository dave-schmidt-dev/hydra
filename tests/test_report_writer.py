"""Tests for hydra.report — post-session report writer (Phase 7 Task 7.1)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hydra import audit, report, state
from hydra.models import ModelSpec
from hydra.quota import NoCandidateModelError, QuotaRouter

HEAVY_PRIMARY = ModelSpec(cli="claude", model="claude-opus-4-7", hard_timeout_s=300.0)
HEAVY_FALLBACK = ModelSpec(
    cli="codex",
    model="gpt-5.5",
    effort_flag="--effort high",
    hard_timeout_s=300.0,
)

SAMPLE_REPORT_MD = (
    "## Meeting summary\n\nWe talked about geography.\n\n"
    "## Questions raised\nQ-001 covered the capital of France.\n\n"
    "## Findings\n- Paris is a city.\n\n"
    "## Suggested follow-ups\n- Read more.\n"
)


@pytest.fixture(autouse=True)
def _reset_breaker() -> None:
    state._reset_breaker_for_tests()
    yield
    state._reset_breaker_for_tests()


def _make_meeting_context(session_dir: Path, *, write: bool = True) -> dict:
    ctx = {
        "meeting_about": "Geography review",
        "participants": ["alice", "bob"],
        "corpus_paths": [],
        "obsidian_export_dir": None,
        "hydra_started_at": "2026-05-13T10:00:00+00:00",
    }
    if write:
        ctx_path = session_dir / "hydra" / "meeting_context.json"
        ctx_path.parent.mkdir(parents=True, exist_ok=True)
        ctx_path.write_text(json.dumps(ctx, indent=2), encoding="utf-8")
    return ctx


def _write_artifact(session_dir: Path, q_id: str, body: str) -> Path:
    research = session_dir / "hydra" / "research"
    research.mkdir(parents=True, exist_ok=True)
    artifact = research / f"{q_id}.md"
    artifact.write_text(body, encoding="utf-8")
    return artifact


def _insert_question(
    session_dir: Path,
    q_id: str,
    *,
    status: str,
    in_report: int = 1,
    topic: str | None = None,
) -> None:
    state.insert_question(
        session_dir,
        q_id=q_id,
        status="pending",
        source="heuristic",
        topic=topic or f"topic for {q_id}",
        rationale=f"rationale {q_id}",
        confidence=0.8,
        transcript_window=f"window {q_id}",
    )
    state.set_question_status(
        session_dir, q_id=q_id, status=status, in_report=in_report
    )


class _FakeProc:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        timeout: bool = False,
        pid: int = 12345,
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
    record: list[dict] | None = None,
):
    iterator = iter(procs)

    async def spawn(argv, *, label, **kwargs):
        if record is not None:
            record.append({"argv": list(argv), "label": label})
        try:
            return next(iterator)
        except StopIteration as exc:  # pragma: no cover - defensive
            raise AssertionError("spawn called more times than scripted") from exc

    return spawn


def _make_timeout_spawn(record: list[dict] | None = None):
    async def spawn(argv, *, label, **kwargs):
        if record is not None:
            record.append({"argv": list(argv), "label": label})
        raise TimeoutError("scripted spawn timeout")

    return spawn


def _make_router(pick: ModelSpec | list[ModelSpec] | None = None) -> QuotaRouter:
    router = QuotaRouter(
        cli_available={"claude", "codex", "gemini", "vibe"},
        fetch=lambda: None,
    )
    if pick is None:
        return router
    seq = pick if isinstance(pick, list) else [pick]
    it = iter(seq)

    def picker(tier):
        try:
            return next(it)
        except StopIteration:
            raise NoCandidateModelError(
                f"scripted pick exhausted for tier {tier!r}"
            ) from None

    router.pick_model = picker  # type: ignore[method-assign]
    return router


def _make_router_raising_no_candidate() -> QuotaRouter:
    router = QuotaRouter(
        cli_available=set(),
        fetch=lambda: None,
    )
    return router


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    state.init_session_db(tmp_path)
    (tmp_path / "hydra" / "research").mkdir(parents=True, exist_ok=True)
    return tmp_path


class TestHappyPath:
    async def test_happy_path_writes_report_and_raw(self, session_dir: Path) -> None:
        _make_meeting_context(session_dir)
        _insert_question(session_dir, "q-001", status="answered", in_report=1)
        _insert_question(session_dir, "q-002", status="dismissed", in_report=1)
        _insert_question(session_dir, "q-003", status="answered", in_report=0)
        _write_artifact(session_dir, "q-001", "## Q-001 body\n[1] cite.")

        proc = _FakeProc(stdout=SAMPLE_REPORT_MD, returncode=0)
        spawn = _make_spawn_stub([proc])
        router = _make_router(HEAVY_PRIMARY)

        result = await report.generate_report(
            session_dir,
            quota_router=router,
            spawn=spawn,
        )

        assert result.error is None
        assert result.questions_in_report == 1
        assert result.questions_pruned == 2
        assert result.report_path.exists()
        assert result.raw_output_path.exists()
        content = result.report_path.read_text(encoding="utf-8")
        assert "Meeting summary" in content
        assert "Paris is a city." in content
        raw = result.raw_output_path.read_text(encoding="utf-8")
        assert raw.strip() == SAMPLE_REPORT_MD.strip()
        assert result.model_spec == HEAVY_PRIMARY
        assert result.duration_s >= 0.0


class TestEmptyMeeting:
    async def test_no_questions_returns_error_no_writes(
        self, session_dir: Path
    ) -> None:
        _make_meeting_context(session_dir)

        spawn_called: list[dict] = []

        async def spawn(argv, *, label, **kwargs):
            spawn_called.append({"argv": list(argv), "label": label})
            raise AssertionError("spawn must not be invoked for empty meetings")

        router = _make_router(HEAVY_PRIMARY)

        result = await report.generate_report(
            session_dir,
            quota_router=router,
            spawn=spawn,
        )

        assert result.error == "no questions to report"
        assert not result.report_path.exists()
        assert not result.raw_output_path.exists()
        assert spawn_called == []
        assert result.questions_in_report == 0


class TestMissingMeetingContext:
    async def test_missing_meeting_context_uses_defaults(
        self, session_dir: Path
    ) -> None:
        _insert_question(session_dir, "q-001", status="answered", in_report=1)
        _write_artifact(session_dir, "q-001", "## Q-001 body\n[1] cite.")

        record: list[dict] = []
        proc = _FakeProc(stdout=SAMPLE_REPORT_MD, returncode=0)
        spawn = _make_spawn_stub([proc], record=record)
        router = _make_router(HEAVY_PRIMARY)

        result = await report.generate_report(
            session_dir,
            quota_router=router,
            spawn=spawn,
        )

        assert result.error is None
        assert result.report_path.exists()
        assert len(record) == 1


class TestSynthesisTimeout:
    async def test_timeout_writes_placeholder_and_raw(self, session_dir: Path) -> None:
        _make_meeting_context(session_dir)
        _insert_question(session_dir, "q-001", status="answered", in_report=1)
        _write_artifact(session_dir, "q-001", "## body")

        spawn = _make_timeout_spawn()
        router = _make_router(HEAVY_PRIMARY)

        result = await report.generate_report(
            session_dir,
            quota_router=router,
            spawn=spawn,
        )

        assert result.error is not None
        assert "timeout" in result.error.lower()
        assert result.report_path.exists()
        assert (
            "synthesis failed" in result.report_path.read_text(encoding="utf-8").lower()
        )
        # Raw file exists for audit even on timeout, may be empty.
        assert result.raw_output_path.exists()


class TestSynthesisNonzeroExit:
    async def test_nonzero_exit_writes_placeholder(self, session_dir: Path) -> None:
        _make_meeting_context(session_dir)
        _insert_question(session_dir, "q-001", status="answered", in_report=1)
        _write_artifact(session_dir, "q-001", "## body")

        proc = _FakeProc(stdout="garbage", stderr="boom", returncode=1)
        spawn = _make_spawn_stub([proc])
        router = _make_router(HEAVY_PRIMARY)

        result = await report.generate_report(
            session_dir,
            quota_router=router,
            spawn=spawn,
        )

        assert result.error is not None
        assert "exit 1" in result.error
        assert result.report_path.exists()
        body = result.report_path.read_text(encoding="utf-8").lower()
        assert "synthesis failed" in body
        # Raw stdout is preserved
        assert result.raw_output_path.exists()
        assert result.raw_output_path.read_text(encoding="utf-8") == "garbage"


class TestSynthesisEmptyStdout:
    async def test_empty_stdout_returns_empty_error(self, session_dir: Path) -> None:
        _make_meeting_context(session_dir)
        _insert_question(session_dir, "q-001", status="answered", in_report=1)
        _write_artifact(session_dir, "q-001", "## body")

        proc = _FakeProc(stdout="", returncode=0)
        spawn = _make_spawn_stub([proc])
        router = _make_router(HEAVY_PRIMARY)

        result = await report.generate_report(
            session_dir,
            quota_router=router,
            spawn=spawn,
        )

        assert result.error is not None
        assert "empty" in result.error.lower()
        # raw file exists (even if empty) for audit
        assert result.raw_output_path.exists()
        assert result.raw_output_path.read_text(encoding="utf-8") == ""


class TestFencedOutputUnwrapped:
    async def test_markdown_fences_stripped(self, session_dir: Path) -> None:
        _make_meeting_context(session_dir)
        _insert_question(session_dir, "q-001", status="answered", in_report=1)
        _write_artifact(session_dir, "q-001", "## body")

        fenced = "```markdown\n# Report\n\nBody here.\n```\n"
        proc = _FakeProc(stdout=fenced, returncode=0)
        spawn = _make_spawn_stub([proc])
        router = _make_router(HEAVY_PRIMARY)

        result = await report.generate_report(
            session_dir,
            quota_router=router,
            spawn=spawn,
        )

        assert result.error is None
        body = result.report_path.read_text(encoding="utf-8")
        assert "```" not in body
        assert "# Report" in body
        assert "Body here." in body

    async def test_plain_fences_stripped(self, session_dir: Path) -> None:
        _make_meeting_context(session_dir)
        _insert_question(session_dir, "q-001", status="answered", in_report=1)
        _write_artifact(session_dir, "q-001", "## body")

        fenced = "```\n# Report\n\nBody.\n```"
        proc = _FakeProc(stdout=fenced, returncode=0)
        spawn = _make_spawn_stub([proc])
        router = _make_router(HEAVY_PRIMARY)

        result = await report.generate_report(
            session_dir,
            quota_router=router,
            spawn=spawn,
        )

        body = result.report_path.read_text(encoding="utf-8")
        assert "```" not in body
        assert "# Report" in body


class TestObsidianExportCopy:
    async def test_obsidian_copy_with_frontmatter(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "session-2026-05-13"
        state.init_session_db(session_dir)
        (session_dir / "hydra" / "research").mkdir(parents=True, exist_ok=True)
        _make_meeting_context(session_dir)
        _insert_question(session_dir, "q-001", status="answered", in_report=1)
        _write_artifact(session_dir, "q-001", "## body")

        proc = _FakeProc(stdout=SAMPLE_REPORT_MD, returncode=0)
        spawn = _make_spawn_stub([proc])
        router = _make_router(HEAVY_PRIMARY)

        vault = tmp_path / "vault"
        vault.mkdir(parents=True, exist_ok=True)

        result = await report.generate_report(
            session_dir,
            quota_router=router,
            obsidian_export_dir=vault,
            spawn=spawn,
        )

        assert result.error is None
        assert result.obsidian_copy_path is not None
        assert result.obsidian_copy_path.exists()
        body = result.obsidian_copy_path.read_text(encoding="utf-8")
        lines = body.splitlines()
        assert lines[0] == "---"
        assert any(line.startswith("date:") for line in lines)
        assert any(
            line.startswith("session:") and "session-2026-05-13" in line
            for line in lines
        )
        assert any(line.startswith("model:") for line in lines)
        assert any(line.startswith("questions:") for line in lines)
        # second --- closing frontmatter
        assert lines.count("---") == 2
        assert "Meeting summary" in body
        # Filename uses session-name
        assert result.obsidian_copy_path.name == "session-2026-05-13-report.md"


class TestPruningHonored:
    async def test_in_report_zero_excluded(self, session_dir: Path) -> None:
        _make_meeting_context(session_dir)
        for i in range(1, 6):
            in_report = 0 if i in (2, 4) else 1
            _insert_question(
                session_dir,
                f"q-{i:03d}",
                status="answered",
                in_report=in_report,
            )
            _write_artifact(session_dir, f"q-{i:03d}", f"## body for q-{i:03d}")

        record: list[dict] = []
        proc = _FakeProc(stdout=SAMPLE_REPORT_MD, returncode=0)
        spawn = _make_spawn_stub([proc], record=record)
        router = _make_router(HEAVY_PRIMARY)

        result = await report.generate_report(
            session_dir,
            quota_router=router,
            spawn=spawn,
        )

        assert result.error is None
        assert result.questions_in_report == 3
        assert result.questions_pruned == 2

        prompt = record[0]["argv"][2]
        assert "q-001" in prompt
        assert "q-003" in prompt
        assert "q-005" in prompt
        assert "q-002" not in prompt
        assert "q-004" not in prompt


class TestArtifactContentInPrompt:
    async def test_artifact_bodies_preserved_verbatim(self, session_dir: Path) -> None:
        _make_meeting_context(session_dir)
        body_001 = "## ANSWER FOR Q-001\nThe answer cites [1] https://x.test/foo"
        body_002 = "## ANSWER FOR Q-002\nLocal cite [2] `/path/to/file.md:42`"
        _insert_question(session_dir, "q-001", status="answered", in_report=1)
        _insert_question(session_dir, "q-002", status="answered", in_report=1)
        _write_artifact(session_dir, "q-001", body_001)
        _write_artifact(session_dir, "q-002", body_002)

        record: list[dict] = []
        proc = _FakeProc(stdout=SAMPLE_REPORT_MD, returncode=0)
        spawn = _make_spawn_stub([proc], record=record)
        router = _make_router(HEAVY_PRIMARY)

        await report.generate_report(
            session_dir,
            quota_router=router,
            spawn=spawn,
        )

        prompt = record[0]["argv"][2]
        assert body_001 in prompt
        assert body_002 in prompt


class TestNoCandidateModel:
    async def test_no_candidate_error_skips_write(self, session_dir: Path) -> None:
        _make_meeting_context(session_dir)
        _insert_question(session_dir, "q-001", status="answered", in_report=1)
        _write_artifact(session_dir, "q-001", "## body")

        router = _make_router_raising_no_candidate()

        async def unused_spawn(*args, **kwargs):
            raise AssertionError("spawn must not be invoked when no candidate")

        result = await report.generate_report(
            session_dir,
            quota_router=router,
            spawn=unused_spawn,
        )

        assert result.error is not None
        assert "no candidate" in result.error.lower()
        assert not result.report_path.exists()


class TestStateConfigUpdated:
    async def test_last_report_at_set_after_happy_path(self, session_dir: Path) -> None:
        _make_meeting_context(session_dir)
        _insert_question(session_dir, "q-001", status="answered", in_report=1)
        _write_artifact(session_dir, "q-001", "## body")

        proc = _FakeProc(stdout=SAMPLE_REPORT_MD, returncode=0)
        spawn = _make_spawn_stub([proc])
        router = _make_router(HEAVY_PRIMARY)

        await report.generate_report(
            session_dir,
            quota_router=router,
            spawn=spawn,
        )

        ts = state.get_config(session_dir, "hydra.last_report_at")
        assert isinstance(ts, str)
        from datetime import datetime

        parsed = datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None


class TestAuditEventEmitted:
    async def test_report_generated_audit_event(self, session_dir: Path) -> None:
        _make_meeting_context(session_dir)
        _insert_question(session_dir, "q-001", status="answered", in_report=1)
        _write_artifact(session_dir, "q-001", "## body")

        proc = _FakeProc(stdout=SAMPLE_REPORT_MD, returncode=0)
        spawn = _make_spawn_stub([proc])
        router = _make_router(HEAVY_PRIMARY)

        await report.generate_report(
            session_dir,
            quota_router=router,
            spawn=spawn,
        )

        rows = audit.tail(session_dir)
        events = [r for r in rows if r.get("event") == "report_generated"]
        assert len(events) == 1
        ev = events[0]
        assert ev["model"] == HEAVY_PRIMARY.to_id()
        assert ev["questions_in_report"] == 1
        assert ev["questions_pruned"] == 0
        assert "duration_s" in ev


class TestInvestigatingIncluded:
    async def test_investigating_status_retained(self, session_dir: Path) -> None:
        _make_meeting_context(session_dir)
        _insert_question(session_dir, "q-001", status="answered", in_report=1)
        _insert_question(session_dir, "q-002", status="investigating", in_report=1)
        _write_artifact(session_dir, "q-001", "## body for q-001")
        _write_artifact(session_dir, "q-002", "## body for q-002")

        record: list[dict] = []
        proc = _FakeProc(stdout=SAMPLE_REPORT_MD, returncode=0)
        spawn = _make_spawn_stub([proc], record=record)
        router = _make_router(HEAVY_PRIMARY)

        result = await report.generate_report(
            session_dir,
            quota_router=router,
            spawn=spawn,
        )

        assert result.questions_in_report == 2
        prompt = record[0]["argv"][2]
        assert "q-001" in prompt
        assert "q-002" in prompt


class TestArgvShape:
    async def test_argv_uses_worker_build_argv(self, session_dir: Path) -> None:
        _make_meeting_context(session_dir)
        _insert_question(session_dir, "q-001", status="answered", in_report=1)
        _write_artifact(session_dir, "q-001", "## body")

        record: list[dict] = []
        proc = _FakeProc(stdout=SAMPLE_REPORT_MD, returncode=0)
        spawn = _make_spawn_stub([proc], record=record)
        router = _make_router(HEAVY_PRIMARY)

        await report.generate_report(
            session_dir,
            quota_router=router,
            spawn=spawn,
        )

        argv = record[0]["argv"]
        assert argv[0] == "claude"
        assert "--model" in argv
        assert HEAVY_PRIMARY.model in argv
        assert record[0]["label"].startswith("report:")
