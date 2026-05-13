"""Two-queue priority coordinator for research workers.

Plan Section 4.5.4: per-flag, enqueue one quick and one deep ``ResearchJob``
onto independent tier queues. Worker tasks pull and run jobs via
``worker.run_research_job``; quick failures do not cancel the matching deep
job, and vice versa.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from hydra import state, worker
from hydra.quota import QuotaRouter
from hydra.watcher import Flag

logger = logging.getLogger("hydra.dispatcher")


@dataclass
class DispatcherConfig:
    quick_concurrency: int = 3
    deep_concurrency: int = 2
    quick_timeout_s: float = 60.0
    deep_timeout_s: float = 300.0


class Dispatcher:
    def __init__(
        self,
        *,
        quota_router: QuotaRouter,
        session_dir: Path,
        meeting_context: str = "",
        config: DispatcherConfig | None = None,
        on_event: worker.WorkerEventSink = None,
        spawn: worker.WorkerSpawn | None = None,
        run_research_job: Callable[..., Awaitable[worker.ResearchJob]] | None = None,
    ) -> None:
        self._quota_router = quota_router
        self._session_dir = session_dir
        self._meeting_context = meeting_context
        self._config = config or DispatcherConfig()
        self._on_event = on_event
        self._spawn = spawn
        self._run_research_job = run_research_job or worker.run_research_job

        self._quick_queue: asyncio.Queue[worker.ResearchJob] = asyncio.Queue()
        self._deep_queue: asyncio.Queue[worker.ResearchJob] = asyncio.Queue()

        self._quick_active = 0
        self._deep_active = 0
        self._quick_completed = 0
        self._deep_completed = 0
        self._quick_failed = 0
        self._deep_failed = 0

        self._stop_requested = False
        self._worker_tasks: list[asyncio.Task] = []
        self._known_q_ids: set[str] = set()

    async def enqueue_flag(self, flag: Flag) -> None:
        """Build a quick + deep ResearchJob for the flag, push both."""
        if flag.q_id not in self._known_q_ids:
            try:
                state.insert_question(
                    self._session_dir,
                    q_id=flag.q_id,
                    status=flag.status,
                    source=flag.source,
                    topic=flag.topic,
                    rationale=flag.rationale,
                    confidence=flag.confidence,
                    transcript_window=flag.transcript_window,
                )
            except Exception as exc:
                logger.warning(
                    "state.insert_question failed for %s: %s", flag.q_id, exc
                )
            self._known_q_ids.add(flag.q_id)

        quick_job = worker.ResearchJob(
            q_id=flag.q_id,
            tier="fast",
            flag=flag,
            session_dir=self._session_dir,
            meeting_context=self._meeting_context,
        )
        deep_job = worker.ResearchJob(
            q_id=flag.q_id,
            tier="heavy",
            flag=flag,
            session_dir=self._session_dir,
            meeting_context=self._meeting_context,
        )
        await self._quick_queue.put(quick_job)
        await self._deep_queue.put(deep_job)

    async def run(self) -> None:
        """Spawn worker tasks; return when stop() is called."""
        self._stop_requested = False
        loop = asyncio.get_running_loop()
        self._worker_tasks = []

        for i in range(self._config.quick_concurrency):
            t = loop.create_task(self._worker_loop("fast", i))
            self._worker_tasks.append(t)
        for i in range(self._config.deep_concurrency):
            t = loop.create_task(self._worker_loop("heavy", i))
            self._worker_tasks.append(t)

        try:
            while not self._stop_requested:
                await asyncio.sleep(0.01)
        finally:
            for t in self._worker_tasks:
                t.cancel()
            for t in self._worker_tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            self._worker_tasks = []

    def stop(self) -> None:
        self._stop_requested = True

    @property
    def stats(self) -> dict:
        return {
            "quick_active": self._quick_active,
            "deep_active": self._deep_active,
            "quick_pending": self._quick_queue.qsize(),
            "deep_pending": self._deep_queue.qsize(),
            "quick_completed": self._quick_completed,
            "deep_completed": self._deep_completed,
            "quick_failed": self._quick_failed,
            "deep_failed": self._deep_failed,
        }

    async def _worker_loop(self, tier: str, slot: int) -> None:
        queue = self._quick_queue if tier == "fast" else self._deep_queue
        while True:
            try:
                job = await queue.get()
            except asyncio.CancelledError:
                return

            if tier == "fast":
                self._quick_active += 1
            else:
                self._deep_active += 1

            try:
                kwargs: dict = {"quota_router": self._quota_router}
                if self._on_event is not None:
                    kwargs["on_event"] = self._on_event
                if self._spawn is not None:
                    kwargs["spawn"] = self._spawn
                result = await self._run_research_job(job, **kwargs)
                self._record_completion(tier, result)
            except asyncio.CancelledError:
                if tier == "fast":
                    self._quick_active = max(0, self._quick_active - 1)
                else:
                    self._deep_active = max(0, self._deep_active - 1)
                return
            except Exception:
                logger.exception(
                    "research job raised for q_id=%s tier=%s slot=%d",
                    job.q_id,
                    tier,
                    slot,
                )
                if tier == "fast":
                    self._quick_failed += 1
                else:
                    self._deep_failed += 1
            finally:
                if tier == "fast":
                    self._quick_active = max(0, self._quick_active - 1)
                else:
                    self._deep_active = max(0, self._deep_active - 1)

    def _record_completion(self, tier: str, result: worker.ResearchJob) -> None:
        if tier == "fast":
            if result.status == "done":
                self._quick_completed += 1
            else:
                self._quick_failed += 1
        else:
            if result.status == "done":
                self._deep_completed += 1
            else:
                self._deep_failed += 1
