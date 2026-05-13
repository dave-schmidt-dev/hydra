"""Tests for watcher failure handling and fallback model switching."""

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


def _q_id_factory() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"q-{next(counter):03d}"


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


def _scripted_invoker(
    script: list,
    seen_models: list[ModelSpec],
) -> Callable[[str, ModelSpec], Awaitable[list[dict]]]:
    it = iter(script)

    async def invoker(prompt: str, model: ModelSpec) -> list[dict]:
        seen_models.append(model)
        try:
            value = next(it)
        except StopIteration:
            return []
        if isinstance(value, Exception):
            raise value
        return value

    return invoker


async def test_first_failure_emits_banner_stays_on_primary(
    flags_collector, banners_collector
) -> None:
    _, on_flag = flags_collector
    banners, on_banner = banners_collector
    queue: asyncio.Queue = asyncio.Queue()
    seen: list[ModelSpec] = []
    invoker = _scripted_invoker([RuntimeError("boom")], seen)
    watcher = Watcher(
        event_queue=queue,
        model_invoker=invoker,
        primary_model=PRIMARY,
        fallback_model=FALLBACK,
        tick_seconds=0.05,
        on_flag=on_flag,
        on_banner=on_banner,
        next_q_id=_q_id_factory(),
        clock=VirtualClock(),
    )

    task = asyncio.create_task(watcher.run())
    try:
        await queue.put({"type": "transcript", "elapsed": 1.0, "text": "x"})
        await asyncio.sleep(0.12)
    finally:
        watcher.stop()
        await asyncio.wait_for(task, timeout=2.0)

    assert any(b.severity == "warning" for b in banners)
    assert watcher.current_model == PRIMARY
    assert watcher.on_fallback is False


async def test_two_failures_in_30s_switch_to_fallback(
    flags_collector, banners_collector
) -> None:
    _, on_flag = flags_collector
    banners, on_banner = banners_collector
    queue: asyncio.Queue = asyncio.Queue()
    clock = VirtualClock()
    seen: list[ModelSpec] = []
    invoker = _scripted_invoker([RuntimeError("boom1"), RuntimeError("boom2")], seen)
    watcher = Watcher(
        event_queue=queue,
        model_invoker=invoker,
        primary_model=PRIMARY,
        fallback_model=FALLBACK,
        tick_seconds=0.05,
        on_flag=on_flag,
        on_banner=on_banner,
        next_q_id=_q_id_factory(),
        clock=clock,
    )

    task = asyncio.create_task(watcher.run())
    try:
        await queue.put({"type": "transcript", "elapsed": 1.0, "text": "a"})
        await asyncio.sleep(0.12)
        clock.advance(5.0)
        await queue.put({"type": "transcript", "elapsed": 2.0, "text": "b"})
        await asyncio.sleep(0.18)
    finally:
        watcher.stop()
        await asyncio.wait_for(task, timeout=2.0)

    assert watcher.current_model == FALLBACK
    assert watcher.on_fallback is True
    assert any(
        b.severity == "info" and "fallback" in b.message.lower() for b in banners
    )


async def test_failures_spaced_beyond_window_do_not_flip(
    flags_collector, banners_collector
) -> None:
    _, on_flag = flags_collector
    banners, on_banner = banners_collector
    queue: asyncio.Queue = asyncio.Queue()
    clock = VirtualClock()

    failures_fired = 0

    async def invoker(prompt: str, model: ModelSpec) -> list[dict]:
        nonlocal failures_fired
        if failures_fired == 0:
            failures_fired = 1
            raise RuntimeError("boom1")
        if failures_fired == 1 and clock() >= 1030.0:
            failures_fired = 2
            raise RuntimeError("boom2")
        return []

    watcher = Watcher(
        event_queue=queue,
        model_invoker=invoker,
        primary_model=PRIMARY,
        fallback_model=FALLBACK,
        tick_seconds=0.05,
        on_flag=on_flag,
        on_banner=on_banner,
        next_q_id=_q_id_factory(),
        clock=clock,
    )

    task = asyncio.create_task(watcher.run())
    try:
        await queue.put({"type": "transcript", "elapsed": 1.0, "text": "a"})
        await asyncio.sleep(0.18)
        clock.advance(40.0)
        await queue.put({"type": "transcript", "elapsed": 2.0, "text": "b"})
        await asyncio.sleep(0.18)
    finally:
        watcher.stop()
        await asyncio.wait_for(task, timeout=2.0)

    assert watcher.current_model == PRIMARY
    assert watcher.on_fallback is False
    assert not any(
        b.severity == "info" and "fallback" in b.message.lower() for b in banners
    )


async def test_successful_tick_resets_failure_counter(
    flags_collector, banners_collector
) -> None:
    _, on_flag = flags_collector
    _, on_banner = banners_collector
    queue: asyncio.Queue = asyncio.Queue()
    clock = VirtualClock()
    seen: list[ModelSpec] = []
    invoker = _scripted_invoker(
        [RuntimeError("boom1"), [], RuntimeError("boom2")], seen
    )
    watcher = Watcher(
        event_queue=queue,
        model_invoker=invoker,
        primary_model=PRIMARY,
        fallback_model=FALLBACK,
        tick_seconds=0.05,
        on_flag=on_flag,
        on_banner=on_banner,
        next_q_id=_q_id_factory(),
        clock=clock,
    )

    task = asyncio.create_task(watcher.run())
    try:
        await queue.put({"type": "transcript", "elapsed": 1.0, "text": "a"})
        await asyncio.sleep(0.12)
        clock.advance(1.0)
        await queue.put({"type": "transcript", "elapsed": 2.0, "text": "b"})
        await asyncio.sleep(0.12)
        clock.advance(1.0)
        await queue.put({"type": "transcript", "elapsed": 3.0, "text": "c"})
        await asyncio.sleep(0.18)
    finally:
        watcher.stop()
        await asyncio.wait_for(task, timeout=2.0)

    assert watcher.current_model == PRIMARY
    assert watcher.on_fallback is False


async def test_already_on_fallback_keeps_trying(
    flags_collector, banners_collector
) -> None:
    _, on_flag = flags_collector
    banners, on_banner = banners_collector
    queue: asyncio.Queue = asyncio.Queue()
    clock = VirtualClock()
    seen: list[ModelSpec] = []
    invoker = _scripted_invoker(
        [
            RuntimeError("boom1"),
            RuntimeError("boom2"),
            RuntimeError("boom3"),
            RuntimeError("boom4"),
        ],
        seen,
    )
    watcher = Watcher(
        event_queue=queue,
        model_invoker=invoker,
        primary_model=PRIMARY,
        fallback_model=FALLBACK,
        tick_seconds=0.05,
        on_flag=on_flag,
        on_banner=on_banner,
        next_q_id=_q_id_factory(),
        clock=clock,
    )

    task = asyncio.create_task(watcher.run())
    try:
        for i in range(4):
            await queue.put(
                {"type": "transcript", "elapsed": float(i + 1), "text": f"t{i}"}
            )
            await asyncio.sleep(0.12)
            clock.advance(2.0)
    finally:
        watcher.stop()
        await asyncio.wait_for(task, timeout=2.0)

    assert watcher.current_model == FALLBACK
    assert watcher.on_fallback is True
    assert any(b.severity == "error" for b in banners)
    fallback_switch_banners = [
        b for b in banners if b.severity == "info" and "fallback" in b.message.lower()
    ]
    assert len(fallback_switch_banners) == 1


async def test_malformed_response_counts_as_failure(
    flags_collector, banners_collector
) -> None:
    _, on_flag = flags_collector
    banners, on_banner = banners_collector
    queue: asyncio.Queue = asyncio.Queue()
    seen: list[ModelSpec] = []

    async def invoker(prompt: str, model: ModelSpec) -> list[dict]:
        seen.append(model)
        return {"not": "a list"}  # type: ignore[return-value]

    watcher = Watcher(
        event_queue=queue,
        model_invoker=invoker,
        primary_model=PRIMARY,
        fallback_model=FALLBACK,
        tick_seconds=0.05,
        on_flag=on_flag,
        on_banner=on_banner,
        next_q_id=_q_id_factory(),
        clock=VirtualClock(),
    )

    task = asyncio.create_task(watcher.run())
    try:
        await queue.put({"type": "transcript", "elapsed": 1.0, "text": "a"})
        await asyncio.sleep(0.12)
    finally:
        watcher.stop()
        await asyncio.wait_for(task, timeout=2.0)

    assert any(b.severity == "warning" for b in banners)
