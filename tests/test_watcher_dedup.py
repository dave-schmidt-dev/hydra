"""Tests for the watcher's flag emission and dedup rules."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable

import pytest

from hydra.models import ModelSpec
from hydra.watcher import Banner, Flag, Watcher

PRIMARY = ModelSpec(cli="claude", model="claude-haiku-4-5", hard_timeout_s=10.0)
FALLBACK = ModelSpec(cli="codex", model="gpt-5.4-mini", hard_timeout_s=10.0)


class VirtualClock:
    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


def _make_q_id_factory() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"q-{next(counter):03d}"


def _make_responder(
    responses: list,
) -> Callable[[str, ModelSpec], Awaitable[list[dict]]]:
    """Build a model_invoker that yields successive responses (or raises)."""
    it = iter(responses)

    async def invoker(prompt: str, model: ModelSpec) -> list[dict]:
        try:
            value = next(it)
        except StopIteration:
            return []
        if isinstance(value, Exception):
            raise value
        return value

    return invoker


async def _drive_ticks(
    watcher: Watcher,
    queue: asyncio.Queue,
    *,
    events_to_inject: list[list[dict]],
    tick_settle: float = 0.06,
    final_settle: float = 0.06,
) -> None:
    """Run the watcher and inject events between ticks.

    Each item in events_to_inject is a list of events to put on the queue
    *before* allowing the next tick to fire.
    """
    task = asyncio.create_task(watcher.run())
    try:
        for batch in events_to_inject:
            for ev in batch:
                await queue.put(ev)
            await asyncio.sleep(tick_settle)
        await asyncio.sleep(final_settle)
    finally:
        watcher.stop()
        await asyncio.wait_for(task, timeout=2.0)


@pytest.fixture
def flags_collector() -> tuple[list[Flag], Callable[[Flag], Awaitable[None]]]:
    flags: list[Flag] = []

    async def on_flag(f: Flag) -> None:
        flags.append(f)

    return flags, on_flag


@pytest.fixture
def banners_collector() -> tuple[list[Banner], Callable[[Banner], Awaitable[None]]]:
    banners: list[Banner] = []

    async def on_banner(b: Banner) -> None:
        banners.append(b)

    return banners, on_banner


async def test_auto_fire_above_threshold(flags_collector, banners_collector) -> None:
    flags, on_flag = flags_collector
    banners, on_banner = banners_collector
    queue: asyncio.Queue = asyncio.Queue()
    clock = VirtualClock()
    invoker = _make_responder(
        [[{"topic": "Pricing tiers", "confidence": 0.9, "rationale": "discussed"}]]
    )
    watcher = Watcher(
        event_queue=queue,
        model_invoker=invoker,
        primary_model=PRIMARY,
        fallback_model=FALLBACK,
        tick_seconds=0.05,
        window_seconds=30.0,
        on_flag=on_flag,
        on_banner=on_banner,
        next_q_id=_make_q_id_factory(),
        clock=clock,
    )
    await _drive_ticks(
        watcher,
        queue,
        events_to_inject=[
            [{"type": "transcript", "elapsed": 1.0, "text": "what about pricing?"}]
        ],
    )

    assert len(flags) == 1
    assert flags[0].status == "pending"
    assert flags[0].topic == "Pricing tiers"
    assert flags[0].confidence == 0.9
    assert flags[0].source == "heuristic"
    assert flags[0].q_id == "q-001"
    assert banners == []


async def test_suggestion_mid_band(flags_collector, banners_collector) -> None:
    flags, on_flag = flags_collector
    _, on_banner = banners_collector
    queue: asyncio.Queue = asyncio.Queue()
    invoker = _make_responder(
        [[{"topic": "Hiring plan", "confidence": 0.5, "rationale": "vague mention"}]]
    )
    watcher = Watcher(
        event_queue=queue,
        model_invoker=invoker,
        primary_model=PRIMARY,
        fallback_model=FALLBACK,
        tick_seconds=0.05,
        on_flag=on_flag,
        on_banner=on_banner,
        next_q_id=_make_q_id_factory(),
        clock=VirtualClock(),
    )
    await _drive_ticks(
        watcher,
        queue,
        events_to_inject=[
            [{"type": "transcript", "elapsed": 1.0, "text": "we need to hire"}]
        ],
    )

    assert len(flags) == 1
    assert flags[0].status == "suggested"


async def test_below_suggest_floor_no_flag(flags_collector, banners_collector) -> None:
    flags, on_flag = flags_collector
    banners, on_banner = banners_collector
    queue: asyncio.Queue = asyncio.Queue()
    invoker = _make_responder(
        [[{"topic": "tiny", "confidence": 0.3, "rationale": "trivial"}]]
    )
    watcher = Watcher(
        event_queue=queue,
        model_invoker=invoker,
        primary_model=PRIMARY,
        fallback_model=FALLBACK,
        tick_seconds=0.05,
        on_flag=on_flag,
        on_banner=on_banner,
        next_q_id=_make_q_id_factory(),
        clock=VirtualClock(),
    )
    await _drive_ticks(
        watcher,
        queue,
        events_to_inject=[
            [{"type": "transcript", "elapsed": 1.0, "text": "anything?"}]
        ],
    )

    assert flags == []
    assert banners == []


async def test_char_jaccard_dedup_high_overlap(
    flags_collector, banners_collector
) -> None:
    flags, on_flag = flags_collector
    _, on_banner = banners_collector
    queue: asyncio.Queue = asyncio.Queue()
    invoker = _make_responder(
        [
            [
                {
                    "topic": "When does the contract renew?",
                    "confidence": 0.85,
                    "rationale": "first",
                }
            ],
            [
                {
                    "topic": "When does the contract get renewed?",
                    "confidence": 0.85,
                    "rationale": "second",
                }
            ],
        ]
    )
    watcher = Watcher(
        event_queue=queue,
        model_invoker=invoker,
        primary_model=PRIMARY,
        fallback_model=FALLBACK,
        tick_seconds=0.05,
        on_flag=on_flag,
        on_banner=on_banner,
        next_q_id=_make_q_id_factory(),
        clock=VirtualClock(),
    )
    await _drive_ticks(
        watcher,
        queue,
        events_to_inject=[
            [{"type": "transcript", "elapsed": 1.0, "text": "contract renew?"}],
            [{"type": "transcript", "elapsed": 2.0, "text": "renewal again"}],
        ],
    )

    assert len(flags) == 1
    assert flags[0].topic == "When does the contract renew?"


async def test_distinct_topics_both_flagged(flags_collector, banners_collector) -> None:
    flags, on_flag = flags_collector
    _, on_banner = banners_collector
    queue: asyncio.Queue = asyncio.Queue()
    invoker = _make_responder(
        [
            [
                {
                    "topic": "Pricing tiers for enterprise",
                    "confidence": 0.85,
                    "rationale": "x",
                }
            ],
            [
                {
                    "topic": "What's the headcount target?",
                    "confidence": 0.85,
                    "rationale": "y",
                }
            ],
        ]
    )
    watcher = Watcher(
        event_queue=queue,
        model_invoker=invoker,
        primary_model=PRIMARY,
        fallback_model=FALLBACK,
        tick_seconds=0.05,
        on_flag=on_flag,
        on_banner=on_banner,
        next_q_id=_make_q_id_factory(),
        clock=VirtualClock(),
    )
    await _drive_ticks(
        watcher,
        queue,
        events_to_inject=[
            [{"type": "transcript", "elapsed": 1.0, "text": "talking pricing"}],
            [{"type": "transcript", "elapsed": 2.0, "text": "talking headcount"}],
        ],
    )

    assert len(flags) == 2
    assert {f.topic for f in flags} == {
        "Pricing tiers for enterprise",
        "What's the headcount target?",
    }


async def test_substring_dedup(flags_collector, banners_collector) -> None:
    flags, on_flag = flags_collector
    _, on_banner = banners_collector
    queue: asyncio.Queue = asyncio.Queue()
    invoker = _make_responder(
        [
            [{"topic": "Authentication flow", "confidence": 0.9, "rationale": "1"}],
            [
                {
                    "topic": "Authentication flow with SSO",
                    "confidence": 0.9,
                    "rationale": "2",
                }
            ],
        ]
    )
    watcher = Watcher(
        event_queue=queue,
        model_invoker=invoker,
        primary_model=PRIMARY,
        fallback_model=FALLBACK,
        tick_seconds=0.05,
        on_flag=on_flag,
        on_banner=on_banner,
        next_q_id=_make_q_id_factory(),
        clock=VirtualClock(),
    )
    await _drive_ticks(
        watcher,
        queue,
        events_to_inject=[
            [{"type": "transcript", "elapsed": 1.0, "text": "auth"}],
            [{"type": "transcript", "elapsed": 2.0, "text": "sso"}],
        ],
    )

    assert len(flags) == 1
    assert flags[0].topic == "Authentication flow"


async def test_dedup_window_expiry_allows_reflag(
    flags_collector, banners_collector
) -> None:
    flags, on_flag = flags_collector
    _, on_banner = banners_collector
    queue: asyncio.Queue = asyncio.Queue()
    clock = VirtualClock()

    async def invoker(prompt: str, model: ModelSpec) -> list[dict]:
        return [{"topic": "Pricing tiers", "confidence": 0.9, "rationale": "r"}]

    watcher = Watcher(
        event_queue=queue,
        model_invoker=invoker,
        primary_model=PRIMARY,
        fallback_model=FALLBACK,
        tick_seconds=0.05,
        dedup_window_seconds=10.0,
        on_flag=on_flag,
        on_banner=on_banner,
        next_q_id=_make_q_id_factory(),
        clock=clock,
    )

    task = asyncio.create_task(watcher.run())
    try:
        await queue.put({"type": "transcript", "elapsed": 1.0, "text": "pricing"})
        await asyncio.sleep(0.18)
        assert len(flags) == 1

        clock.advance(20.0)
        await queue.put({"type": "transcript", "elapsed": 2.0, "text": "pricing again"})
        await asyncio.sleep(0.18)
    finally:
        watcher.stop()
        await asyncio.wait_for(task, timeout=2.0)

    assert len(flags) == 2


async def test_missing_required_fields_dropped(
    flags_collector, banners_collector
) -> None:
    flags, on_flag = flags_collector
    banners, on_banner = banners_collector
    queue: asyncio.Queue = asyncio.Queue()
    invoker = _make_responder([[{"topic": "only-topic-no-confidence"}]])
    watcher = Watcher(
        event_queue=queue,
        model_invoker=invoker,
        primary_model=PRIMARY,
        fallback_model=FALLBACK,
        tick_seconds=0.05,
        on_flag=on_flag,
        on_banner=on_banner,
        next_q_id=_make_q_id_factory(),
        clock=VirtualClock(),
    )
    await _drive_ticks(
        watcher,
        queue,
        events_to_inject=[[{"type": "transcript", "elapsed": 1.0, "text": "thing"}]],
    )

    assert flags == []
    assert banners == []


async def test_empty_model_response_no_flags(
    flags_collector, banners_collector
) -> None:
    flags, on_flag = flags_collector
    banners, on_banner = banners_collector
    queue: asyncio.Queue = asyncio.Queue()
    invoker = _make_responder([[]])
    watcher = Watcher(
        event_queue=queue,
        model_invoker=invoker,
        primary_model=PRIMARY,
        fallback_model=FALLBACK,
        tick_seconds=0.05,
        on_flag=on_flag,
        on_banner=on_banner,
        next_q_id=_make_q_id_factory(),
        clock=VirtualClock(),
    )
    await _drive_ticks(
        watcher,
        queue,
        events_to_inject=[[{"type": "transcript", "elapsed": 1.0, "text": "thing"}]],
    )

    assert flags == []
    assert banners == []


async def test_empty_window_skips_tick(flags_collector, banners_collector) -> None:
    flags, on_flag = flags_collector
    banners, on_banner = banners_collector
    queue: asyncio.Queue = asyncio.Queue()

    call_count = 0

    async def invoker(prompt: str, model: ModelSpec) -> list[dict]:
        nonlocal call_count
        call_count += 1
        return []

    watcher = Watcher(
        event_queue=queue,
        model_invoker=invoker,
        primary_model=PRIMARY,
        fallback_model=FALLBACK,
        tick_seconds=0.05,
        on_flag=on_flag,
        on_banner=on_banner,
        next_q_id=_make_q_id_factory(),
        clock=VirtualClock(),
    )

    task = asyncio.create_task(watcher.run())
    try:
        await asyncio.sleep(0.25)
    finally:
        watcher.stop()
        await asyncio.wait_for(task, timeout=2.0)

    assert call_count == 0
    assert flags == []
    assert banners == []


async def test_non_transcript_events_are_ignored(
    flags_collector, banners_collector
) -> None:
    _flags, on_flag = flags_collector
    _, on_banner = banners_collector
    queue: asyncio.Queue = asyncio.Queue()

    call_count = 0

    async def invoker(prompt: str, model: ModelSpec) -> list[dict]:
        nonlocal call_count
        call_count += 1
        return []

    watcher = Watcher(
        event_queue=queue,
        model_invoker=invoker,
        primary_model=PRIMARY,
        fallback_model=FALLBACK,
        tick_seconds=0.05,
        on_flag=on_flag,
        on_banner=on_banner,
        next_q_id=_make_q_id_factory(),
        clock=VirtualClock(),
    )

    task = asyncio.create_task(watcher.run())
    try:
        await queue.put({"type": "warning", "elapsed": 1.0, "message": "x"})
        await queue.put({"type": "segment_boundary", "elapsed": 2.0})
        await asyncio.sleep(0.25)
    finally:
        watcher.stop()
        await asyncio.wait_for(task, timeout=2.0)

    assert call_count == 0
