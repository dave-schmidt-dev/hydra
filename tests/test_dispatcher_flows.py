"""Tests for hydra.dispatcher — two-tier priority coordinator (Phase 4 Task 4.2)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from hydra import dispatcher, state, worker
from hydra.dispatcher import Dispatcher, DispatcherConfig
from hydra.models import ModelSpec
from hydra.quota import QuotaRouter
from hydra.watcher import Flag

FAST_PRIMARY = ModelSpec(cli="claude", model="claude-haiku-4-5", hard_timeout_s=60.0)
HEAVY_PRIMARY = ModelSpec(cli="claude", model="claude-opus-4-7", hard_timeout_s=300.0)


def _flag(q_id: str = "q-001", topic: str = "Cap of France") -> Flag:
    return Flag(
        q_id=q_id,
        topic=topic,
        rationale="user asked",
        confidence=0.85,
        transcript_window="what is the capital of france?",
        status="pending",
    )


def _make_router() -> QuotaRouter:
    return QuotaRouter(
        tiers={
            "fast": [FAST_PRIMARY],
            "heavy": [HEAVY_PRIMARY],
            "watcher": [FAST_PRIMARY],
        },
        cli_available={"claude"},
        fetch=lambda: None,
    )


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    state._reset_breaker_for_tests()
    state.init_session_db(tmp_path)
    return tmp_path


def make_dispatcher(
    session_dir: Path,
    *,
    stub_run,
    config: DispatcherConfig | None = None,
) -> Dispatcher:
    return Dispatcher(
        quota_router=_make_router(),
        session_dir=session_dir,
        meeting_context="test meeting",
        config=config or DispatcherConfig(quick_concurrency=2, deep_concurrency=1),
        run_research_job=stub_run,
    )


async def _drain(disp: Dispatcher, *, max_wait: float = 2.0) -> None:
    """Spin up the dispatcher, wait for queues to drain, then stop."""
    task = asyncio.create_task(disp.run())
    try:
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            stats = disp.stats
            if (
                stats["quick_pending"] == 0
                and stats["deep_pending"] == 0
                and stats["quick_active"] == 0
                and stats["deep_active"] == 0
            ):
                break
            await asyncio.sleep(0.01)
    finally:
        disp.stop()
        await asyncio.wait_for(task, timeout=2.0)


class TestEnqueue:
    async def test_flag_enqueues_one_quick_and_one_deep(
        self, session_dir: Path
    ) -> None:
        calls: list[tuple[str, str]] = []

        async def stub_run(job: worker.ResearchJob, **kwargs) -> worker.ResearchJob:
            calls.append((job.q_id, job.tier))
            job.status = "done"
            return job

        disp = make_dispatcher(session_dir, stub_run=stub_run)
        await disp.enqueue_flag(_flag("q-001"))
        await _drain(disp)

        tiers_seen = {t for _, t in calls}
        assert tiers_seen == {"fast", "heavy"}
        q_ids_seen = {q for q, _ in calls}
        assert q_ids_seen == {"q-001"}


class TestIndependentTierFailures:
    async def test_quick_failure_does_not_cancel_deep(self, session_dir: Path) -> None:
        async def stub_run(job: worker.ResearchJob, **kwargs) -> worker.ResearchJob:
            if job.tier == "fast":
                job.status = "failed"
                job.error = "boom"
            else:
                job.status = "done"
            return job

        disp = make_dispatcher(session_dir, stub_run=stub_run)
        await disp.enqueue_flag(_flag("q-001"))
        await _drain(disp)

        stats = disp.stats
        assert stats["quick_failed"] == 1
        assert stats["deep_completed"] == 1
        assert stats["deep_failed"] == 0


class TestConcurrencyCaps:
    async def test_fast_concurrency_cap_respected(self, session_dir: Path) -> None:
        max_observed = {"v": 0}
        active = {"v": 0}
        lock = asyncio.Lock()

        async def stub_run(job: worker.ResearchJob, **kwargs) -> worker.ResearchJob:
            if job.tier != "fast":
                job.status = "done"
                return job
            async with lock:
                active["v"] += 1
                if active["v"] > max_observed["v"]:
                    max_observed["v"] = active["v"]
            try:
                await asyncio.sleep(0.05)
            finally:
                async with lock:
                    active["v"] -= 1
            job.status = "done"
            return job

        disp = make_dispatcher(
            session_dir,
            stub_run=stub_run,
            config=DispatcherConfig(quick_concurrency=2, deep_concurrency=1),
        )
        for i in range(1, 6):
            await disp.enqueue_flag(_flag(f"q-{i:03d}", topic=f"topic-{i}"))
        await _drain(disp, max_wait=5.0)

        assert max_observed["v"] <= 2
        assert max_observed["v"] >= 1

    async def test_deep_concurrency_cap_respected(self, session_dir: Path) -> None:
        max_observed = {"v": 0}
        active = {"v": 0}
        lock = asyncio.Lock()

        async def stub_run(job: worker.ResearchJob, **kwargs) -> worker.ResearchJob:
            if job.tier != "heavy":
                job.status = "done"
                return job
            async with lock:
                active["v"] += 1
                if active["v"] > max_observed["v"]:
                    max_observed["v"] = active["v"]
            try:
                await asyncio.sleep(0.05)
            finally:
                async with lock:
                    active["v"] -= 1
            job.status = "done"
            return job

        disp = make_dispatcher(
            session_dir,
            stub_run=stub_run,
            config=DispatcherConfig(quick_concurrency=3, deep_concurrency=1),
        )
        for i in range(1, 5):
            await disp.enqueue_flag(_flag(f"q-{i:03d}", topic=f"topic-{i}"))
        await _drain(disp, max_wait=5.0)

        assert max_observed["v"] == 1


class TestStop:
    async def test_stop_cancels_workers_cleanly(self, session_dir: Path) -> None:
        async def slow_run(job: worker.ResearchJob, **kwargs) -> worker.ResearchJob:
            await asyncio.sleep(5.0)
            job.status = "done"
            return job

        disp = make_dispatcher(session_dir, stub_run=slow_run)
        for i in range(1, 4):
            await disp.enqueue_flag(_flag(f"q-{i:03d}", topic=f"topic-{i}"))

        task = asyncio.create_task(disp.run())
        await asyncio.sleep(0.05)
        disp.stop()
        await asyncio.wait_for(task, timeout=1.0)


class TestStateInsert:
    async def test_question_row_exists_before_workers_run(
        self, session_dir: Path
    ) -> None:
        observed_q_ids_at_run_time: list[set[str]] = []

        async def stub_run(job: worker.ResearchJob, **kwargs) -> worker.ResearchJob:
            conn = state.open_session_db(session_dir)
            try:
                rows = conn.execute("SELECT q_id FROM questions").fetchall()
            finally:
                conn.close()
            observed_q_ids_at_run_time.append({r[0] for r in rows})
            job.status = "done"
            return job

        disp = make_dispatcher(session_dir, stub_run=stub_run)
        await disp.enqueue_flag(_flag("q-042", topic="something"))
        await _drain(disp)

        assert observed_q_ids_at_run_time, "stub_run was not invoked"
        for ids in observed_q_ids_at_run_time:
            assert "q-042" in ids


class TestStatsShape:
    async def test_stats_keys_present(self, session_dir: Path) -> None:
        async def stub_run(job: worker.ResearchJob, **kwargs) -> worker.ResearchJob:
            job.status = "done"
            return job

        disp = make_dispatcher(session_dir, stub_run=stub_run)
        stats = disp.stats
        for key in (
            "quick_active",
            "deep_active",
            "quick_pending",
            "deep_pending",
            "quick_completed",
            "deep_completed",
            "quick_failed",
            "deep_failed",
        ):
            assert key in stats


def test_dispatcher_config_defaults() -> None:
    cfg = DispatcherConfig()
    assert cfg.quick_concurrency == 3
    assert cfg.deep_concurrency == 2
    assert cfg.quick_timeout_s == 60.0
    assert cfg.deep_timeout_s == 300.0


def test_module_imports() -> None:
    assert hasattr(dispatcher, "Dispatcher")
    assert hasattr(dispatcher, "DispatcherConfig")
