"""Rolling-window topic flagger that consumes the tailer queue.

Watcher ticks every ``tick_seconds`` to snapshot the rolling transcript window,
prompt an injected model invoker, and emit ``Flag`` candidates via ``on_flag``.
Failures surface as ``Banner`` events; two failures within 30s trip the primary
model to the fallback (per plan PM-3).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from hydra.models import ModelSpec

logger = logging.getLogger("hydra.watcher")

Severity = Literal["info", "warning", "error"]
FlagStatus = Literal["pending", "suggested"]


@dataclass(frozen=True)
class Flag:
    q_id: str
    topic: str
    rationale: str
    confidence: float
    transcript_window: str
    status: FlagStatus
    source: str = "heuristic"


@dataclass(frozen=True)
class Banner:
    severity: Severity
    message: str
    monotonic_ts: float


ModelInvoker = Callable[[str, ModelSpec], Awaitable[list[dict]]]

_FAILURE_WINDOW_SECONDS: float = 30.0

_q_counter = itertools.count(1)


def _default_next_q_id() -> str:
    return f"q-{next(_q_counter):03d}"


class Watcher:
    def __init__(
        self,
        *,
        event_queue: asyncio.Queue,
        model_invoker: ModelInvoker,
        primary_model: ModelSpec,
        fallback_model: ModelSpec,
        meeting_context: str = "",
        tick_seconds: float = 15.0,
        window_seconds: float = 30.0,
        dedup_window_seconds: float = 300.0,
        auto_fire_threshold: float = 0.7,
        suggest_floor: float = 0.4,
        on_flag: Callable[[Flag], Awaitable[None]] | None = None,
        on_banner: Callable[[Banner], Awaitable[None]] | None = None,
        next_q_id: Callable[[], str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._queue = event_queue
        self._invoker = model_invoker
        self._primary_model = primary_model
        self._fallback_model = fallback_model
        self._meeting_context = meeting_context
        self._tick_seconds = tick_seconds
        self._window_seconds = window_seconds
        self._dedup_window_seconds = dedup_window_seconds
        self._auto_fire_threshold = auto_fire_threshold
        self._suggest_floor = suggest_floor
        self._on_flag = on_flag
        self._on_banner = on_banner
        self._next_q_id = next_q_id or _default_next_q_id
        self._clock = clock or time.monotonic

        self._window: deque[dict] = deque()
        self._recent_topics: list[tuple[str, float]] = []
        self._failure_times: list[float] = []

        self._current_model = primary_model
        self._on_fallback = False
        self._stop_requested = False

    @property
    def current_model(self) -> ModelSpec:
        return self._current_model

    @property
    def on_fallback(self) -> bool:
        return self._on_fallback

    def stop(self) -> None:
        self._stop_requested = True

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        next_tick = loop.time() + self._tick_seconds
        while not self._stop_requested:
            timeout = max(0.0, next_tick - loop.time())
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            except TimeoutError:
                await self._on_tick()
                next_tick = loop.time() + self._tick_seconds
                continue
            self._ingest(event)

    def _ingest(self, event: dict) -> None:
        if event.get("type") != "transcript":
            return
        self._window.append(event)
        latest = event.get("elapsed")
        if not isinstance(latest, int | float):
            return
        cutoff = float(latest) - self._window_seconds
        while self._window and float(self._window[0].get("elapsed", 0.0)) < cutoff:
            self._window.popleft()

    async def _on_tick(self) -> None:
        # Empty window means no transcript activity this interval; skip the
        # LLM call and do not record a failure.
        if not self._window:
            return

        snapshot = list(self._window)
        prompt = self._build_prompt(snapshot)
        try:
            raw = await self._invoker(prompt, self._current_model)
            if not isinstance(raw, list):
                raise ValueError(f"expected list, got {type(raw).__name__}")
        except Exception as exc:
            logger.warning("watcher invocation failed: %s", exc)
            await self._handle_failure(str(exc))
            return

        self._failure_times.clear()
        await self._process_candidates(raw, snapshot)

    async def _process_candidates(
        self, candidates: list[dict], window_events: list[dict]
    ) -> None:
        window_text = self._render_window(window_events)
        now = self._clock()
        self._prune_recent_topics(now)

        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            topic = cand.get("topic")
            confidence = cand.get("confidence")
            if not isinstance(topic, str) or not topic:
                continue
            if not isinstance(confidence, int | float):
                continue
            conf = float(confidence)
            rationale = cand.get("rationale") or ""
            if not isinstance(rationale, str):
                rationale = str(rationale)

            if conf < self._suggest_floor:
                continue

            recent_topic_strs = [t for t, _ts in self._recent_topics]
            if self._is_duplicate(topic, recent_topic_strs):
                continue

            status: FlagStatus = (
                "pending" if conf >= self._auto_fire_threshold else "suggested"
            )

            self._recent_topics.append((topic, now))
            flag = Flag(
                q_id=self._next_q_id(),
                topic=topic,
                rationale=rationale,
                confidence=conf,
                transcript_window=window_text,
                status=status,
            )
            if self._on_flag is not None:
                await self._on_flag(flag)

    def _prune_recent_topics(self, now: float) -> None:
        cutoff = now - self._dedup_window_seconds
        self._recent_topics = [
            (t, ts) for (t, ts) in self._recent_topics if ts >= cutoff
        ]

    async def _handle_failure(self, message: str) -> None:
        now = self._clock()
        self._failure_times.append(now)
        cutoff = now - _FAILURE_WINDOW_SECONDS
        self._failure_times = [t for t in self._failure_times if t >= cutoff]

        await self._emit_banner(
            Banner(
                severity="warning",
                message=f"watcher unstable: {message}",
                monotonic_ts=now,
            )
        )

        if len(self._failure_times) >= 2:
            if not self._on_fallback:
                self._current_model = self._fallback_model
                self._on_fallback = True
                await self._emit_banner(
                    Banner(
                        severity="info",
                        message=(
                            f"switched to fallback model {self._fallback_model.to_id()}"
                        ),
                        monotonic_ts=self._clock(),
                    )
                )
            else:
                await self._emit_banner(
                    Banner(
                        severity="error",
                        message=(
                            "fallback model failing repeatedly; "
                            "no further models available"
                        ),
                        monotonic_ts=self._clock(),
                    )
                )
            self._failure_times.clear()

    async def _emit_banner(self, banner: Banner) -> None:
        if self._on_banner is not None:
            await self._on_banner(banner)

    def _render_window(self, window_events: list[dict]) -> str:
        return "\n".join(
            f"[{float(e.get('elapsed', 0.0)):.1f}s] {e.get('text', '')}"
            for e in window_events
        )

    def _build_prompt(self, window_events: list[dict]) -> str:
        transcript_lines = self._render_window(window_events)
        return (
            f"Meeting context: {self._meeting_context}\n\n"
            f"Recent transcript:\n{transcript_lines}\n\n"
            "Return a JSON array of {topic, confidence, rationale} objects "
            "for any open questions or discussion-worthy topics raised in the "
            "transcript. Empty array if none."
        )

    @staticmethod
    def _char_jaccard(a: str, b: str) -> float:
        sa, sb = set(a.lower()), set(b.lower())
        if not sa and not sb:
            return 1.0
        union = sa | sb
        if not union:
            return 0.0
        return len(sa & sb) / len(union)

    @staticmethod
    def _is_duplicate(topic: str, recent_topics: list[str]) -> bool:
        # Substring catches "X" -> "X with Y" expansions that char-Jaccard
        # can miss when the new topic is significantly longer.
        t = topic.lower().strip()
        for r in recent_topics:
            rl = r.lower().strip()
            if not t or not rl:
                continue
            if t in rl or rl in t:
                return True
            if Watcher._char_jaccard(t, rl) >= 0.5:
                return True
        return False
