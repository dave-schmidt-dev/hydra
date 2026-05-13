"""Tests for the async transcript tailer (hydra.tailer)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from hydra import state, tailer


@pytest.fixture(autouse=True)
def _reset_breaker() -> None:
    state._reset_breaker_for_tests()
    yield
    state._reset_breaker_for_tests()


def _write_jsonl_line(path: Path, event: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")
        fh.flush()


def _write_raw_line(path: Path, raw: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(raw + "\n")
        fh.flush()


async def _drain_queue(
    q: asyncio.Queue, expected: int, timeout: float = 2.0
) -> list[dict]:
    out: list[dict] = []
    loop_deadline = asyncio.get_event_loop().time() + timeout
    while len(out) < expected:
        remaining = loop_deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        try:
            ev = await asyncio.wait_for(q.get(), timeout=remaining)
        except TimeoutError:
            break
        out.append(ev)
    return out


async def _run_for(t: tailer.TranscriptTailer, seconds: float) -> None:
    task = asyncio.create_task(t.run())
    try:
        await asyncio.sleep(seconds)
    finally:
        t.stop()
        await asyncio.wait_for(task, timeout=2.0)


async def test_append_follow_emits_existing_then_appended(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.touch()
    for i in range(3):
        _write_jsonl_line(
            transcript, {"type": "transcript", "elapsed": float(i + 1), "text": f"a{i}"}
        )

    t = tailer.TranscriptTailer(transcript, poll_interval_s=0.05)
    task = asyncio.create_task(t.run())
    try:
        first_batch = await _drain_queue(t.queue, 3, timeout=2.0)
        assert len(first_batch) == 3
        assert [e["text"] for e in first_batch] == ["a0", "a1", "a2"]

        for i in range(3, 5):
            _write_jsonl_line(
                transcript,
                {"type": "transcript", "elapsed": float(i + 1), "text": f"a{i}"},
            )

        second_batch = await _drain_queue(t.queue, 2, timeout=2.0)
        assert [e["text"] for e in second_batch] == ["a3", "a4"]
    finally:
        t.stop()
        await asyncio.wait_for(task, timeout=2.0)

    assert t.stats.events_emitted == 5


async def test_malformed_line_is_dropped(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.touch()
    for i in range(3):
        _write_jsonl_line(
            transcript, {"type": "transcript", "elapsed": float(i + 1), "text": f"v{i}"}
        )
    _write_raw_line(transcript, "{bad-json")
    for i in range(3, 5):
        _write_jsonl_line(
            transcript, {"type": "transcript", "elapsed": float(i + 1), "text": f"v{i}"}
        )

    t = tailer.TranscriptTailer(transcript, poll_interval_s=0.05)
    task = asyncio.create_task(t.run())
    try:
        events = await _drain_queue(t.queue, 5, timeout=2.0)
    finally:
        t.stop()
        await asyncio.wait_for(task, timeout=2.0)

    assert len(events) == 5
    assert t.stats.events_emitted == 5
    assert t.stats.events_dropped_malformed == 1
    assert t.stats.events_dropped_unknown == 0


async def test_unknown_event_type_is_dropped(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.touch()
    _write_jsonl_line(transcript, {"type": "transcript", "elapsed": 1.0, "text": "a"})
    _write_jsonl_line(transcript, {"type": "transcript", "elapsed": 2.0, "text": "b"})
    _write_jsonl_line(transcript, {"type": "unknown_future_event", "elapsed": 3.0})
    _write_jsonl_line(transcript, {"type": "transcript", "elapsed": 4.0, "text": "c"})
    _write_jsonl_line(transcript, {"type": "transcript", "elapsed": 5.0, "text": "d"})

    t = tailer.TranscriptTailer(transcript, poll_interval_s=0.05)
    task = asyncio.create_task(t.run())
    try:
        events = await _drain_queue(t.queue, 4, timeout=2.0)
    finally:
        t.stop()
        await asyncio.wait_for(task, timeout=2.0)

    assert [e["text"] for e in events] == ["a", "b", "c", "d"]
    assert t.stats.events_emitted == 4
    assert t.stats.events_dropped_unknown == 1
    assert t.stats.events_dropped_malformed == 0


async def test_segment_boundary_does_not_reopen_file(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.touch()
    _write_jsonl_line(transcript, {"type": "transcript", "elapsed": 1.0, "text": "pre"})
    _write_jsonl_line(transcript, {"type": "segment_boundary", "elapsed": 1.5})
    _write_jsonl_line(
        transcript, {"type": "transcript", "elapsed": 2.0, "text": "between"}
    )

    t = tailer.TranscriptTailer(transcript, poll_interval_s=0.05)
    task = asyncio.create_task(t.run())
    try:
        first = await _drain_queue(t.queue, 3, timeout=2.0)
        assert [e["type"] for e in first] == [
            "transcript",
            "segment_boundary",
            "transcript",
        ]

        _write_jsonl_line(
            transcript, {"type": "transcript", "elapsed": 3.0, "text": "after"}
        )
        more = await _drain_queue(t.queue, 1, timeout=2.0)
        assert len(more) == 1
        assert more[0]["text"] == "after"
    finally:
        t.stop()
        await asyncio.wait_for(task, timeout=2.0)


async def test_bounded_queue_drops_oldest_on_overflow(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.touch()
    for i in range(5):
        _write_jsonl_line(
            transcript,
            {"type": "transcript", "elapsed": float(i + 1), "text": f"x{i}"},
        )

    t = tailer.TranscriptTailer(transcript, queue_maxsize=2, poll_interval_s=0.05)
    task = asyncio.create_task(t.run())
    try:
        await asyncio.sleep(0.4)
        t.stop()
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        if not task.done():
            t.stop()
            await asyncio.wait_for(task, timeout=2.0)

    remaining: list[dict] = []
    while not t.queue.empty():
        remaining.append(t.queue.get_nowait())

    assert len(remaining) == 2
    assert [e["text"] for e in remaining] == ["x3", "x4"]
    assert t.stats.events_dropped_queue_full == 3


async def test_resume_cursor_skips_old_events(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.touch()
    for i in range(5):
        _write_jsonl_line(
            transcript,
            {"type": "transcript", "elapsed": float(i + 1), "text": f"e{i + 1}"},
        )

    t = tailer.TranscriptTailer(
        transcript, start_after_elapsed=3.0, poll_interval_s=0.05
    )
    task = asyncio.create_task(t.run())
    try:
        events = await _drain_queue(t.queue, 2, timeout=2.0)
    finally:
        t.stop()
        await asyncio.wait_for(task, timeout=2.0)

    assert [e["elapsed"] for e in events] == [4.0, 5.0]
    assert t.stats.events_emitted == 2


async def test_cursor_persists_on_time_threshold(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.touch()
    state.init_session_db(tmp_path)

    t = tailer.TranscriptTailer(
        transcript,
        session_dir=tmp_path,
        persist_every_seconds=0.05,
        persist_every_events=10_000,
        poll_interval_s=0.05,
    )
    task = asyncio.create_task(t.run())
    try:
        _write_jsonl_line(
            transcript, {"type": "transcript", "elapsed": 7.5, "text": "z"}
        )
        await _drain_queue(t.queue, 1, timeout=2.0)
        await asyncio.sleep(0.4)
    finally:
        t.stop()
        await asyncio.wait_for(task, timeout=2.0)

    stored = state.get_config(tmp_path, "tailer.last_event_elapsed")
    assert stored == 7.5


async def test_cursor_persists_on_event_count_threshold(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.touch()
    state.init_session_db(tmp_path)

    t = tailer.TranscriptTailer(
        transcript,
        session_dir=tmp_path,
        persist_every_seconds=999.0,
        persist_every_events=3,
        poll_interval_s=0.05,
    )
    task = asyncio.create_task(t.run())
    try:
        for i in range(3):
            _write_jsonl_line(
                transcript,
                {"type": "transcript", "elapsed": float(i + 1), "text": f"a{i}"},
            )
        await _drain_queue(t.queue, 3, timeout=2.0)
        await asyncio.sleep(0.2)
        first = state.get_config(tmp_path, "tailer.last_event_elapsed")
        assert first == 3.0

        for i in range(3, 6):
            _write_jsonl_line(
                transcript,
                {"type": "transcript", "elapsed": float(i + 1), "text": f"a{i}"},
            )
        await _drain_queue(t.queue, 3, timeout=2.0)
        await asyncio.sleep(0.2)
        second = state.get_config(tmp_path, "tailer.last_event_elapsed")
        assert second == 6.0
    finally:
        t.stop()
        await asyncio.wait_for(task, timeout=2.0)


async def test_stop_is_idempotent_and_clean(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.touch()
    _write_jsonl_line(transcript, {"type": "transcript", "elapsed": 1.0, "text": "hi"})

    t = tailer.TranscriptTailer(transcript, poll_interval_s=0.05)
    task = asyncio.create_task(t.run())
    await _drain_queue(t.queue, 1, timeout=2.0)
    t.stop()
    t.stop()  # second call must be a no-op
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()
