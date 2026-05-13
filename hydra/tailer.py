"""Async JSONL tailer for Scarecrow's transcript.jsonl."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hydra import state

logger = logging.getLogger("hydra.tailer")

KNOWN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "transcript",
        "divider",
        "pause",
        "resume",
        "note",
        "warning",
        "session_metrics",
        "session_renamed",
        "segment_boundary",
        "input_device_changed",
        "session_start",
        "session_end",
        "recording_start",
    }
)


@dataclass(frozen=True)
class TailerStats:
    events_emitted: int
    events_dropped_malformed: int
    events_dropped_unknown: int
    events_dropped_queue_full: int
    last_event_elapsed: float | None


class TranscriptTailer:
    """Async JSONL tailer for Scarecrow's transcript.jsonl.

    Append-follows the file from a byte offset, validates each line as a JSON
    object with a ``type`` field in :data:`KNOWN_EVENT_TYPES`, and pushes
    parsed events into a bounded ``asyncio.Queue`` with drop-oldest semantics
    on overflow. Persists ``tailer.last_event_elapsed`` to the session state
    DB every ``persist_every_seconds`` OR ``persist_every_events`` (whichever
    fires first).
    """

    def __init__(
        self,
        transcript_path: Path,
        *,
        queue_maxsize: int = 10_000,
        session_dir: Path | None = None,
        start_after_elapsed: float | None = None,
        persist_every_seconds: float = 5.0,
        persist_every_events: int = 100,
        poll_interval_s: float = 0.25,
    ) -> None:
        self._path = Path(transcript_path)
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=queue_maxsize)
        self._session_dir = session_dir
        self._start_after_elapsed = start_after_elapsed
        self._persist_every_seconds = persist_every_seconds
        self._persist_every_events = persist_every_events
        self._poll_interval_s = poll_interval_s

        self._stop_requested = False
        self._past_resume_cursor = start_after_elapsed is None

        self._events_emitted = 0
        self._events_dropped_malformed = 0
        self._events_dropped_unknown = 0
        self._events_dropped_queue_full = 0
        self._last_emitted_elapsed: float | None = None

        self._events_since_persist = 0
        self._last_persist_ts = time.monotonic()
        self._last_persisted_elapsed: float | None = None

        self._offset = 0
        self._partial = b""

        self._wake_event: asyncio.Event | None = None
        self._observer = None

    @property
    def queue(self) -> asyncio.Queue[dict]:
        return self._queue

    @property
    def stats(self) -> TailerStats:
        return TailerStats(
            events_emitted=self._events_emitted,
            events_dropped_malformed=self._events_dropped_malformed,
            events_dropped_unknown=self._events_dropped_unknown,
            events_dropped_queue_full=self._events_dropped_queue_full,
            last_event_elapsed=self._last_emitted_elapsed,
        )

    def stop(self) -> None:
        self._stop_requested = True
        if self._wake_event is not None:
            with contextlib.suppress(RuntimeError):
                self._wake_event.set()

    async def run(self) -> None:
        self._wake_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        self._observer = self._try_install_watchdog(loop)

        try:
            while not self._stop_requested:
                await self._drain_file()
                if self._stop_requested:
                    break
                await self._wait_for_change()
            await self._drain_file()
            await self._maybe_persist_cursor(force=True)
        finally:
            if self._observer is not None:
                try:
                    self._observer.stop()
                    self._observer.join(timeout=1.0)
                except Exception:
                    logger.debug("watchdog observer shutdown failed", exc_info=True)
                self._observer = None

    def _try_install_watchdog(self, loop: asyncio.AbstractEventLoop):
        try:
            from watchdog.events import PatternMatchingEventHandler
            from watchdog.observers import Observer
        except Exception:
            logger.debug("watchdog unavailable; falling back to polling")
            return None

        wake_event = self._wake_event
        assert wake_event is not None

        # watchdog runs the handler in its own thread; bridge to the loop's
        # asyncio.Event via call_soon_threadsafe.
        def _signal() -> None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(wake_event.set)

        class _Handler(PatternMatchingEventHandler):
            def on_modified(self, event):  # type: ignore[override]
                _signal()

            def on_created(self, event):  # type: ignore[override]
                _signal()

        handler = _Handler(patterns=[str(self._path)], ignore_directories=True)
        try:
            observer = Observer()
            observer.schedule(handler, str(self._path.parent), recursive=False)
            observer.start()
        except Exception:
            logger.debug("watchdog observer failed to start", exc_info=True)
            return None
        return observer

    async def _wait_for_change(self) -> None:
        assert self._wake_event is not None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                self._wake_event.wait(), timeout=self._poll_interval_s
            )
        self._wake_event = asyncio.Event()

    async def _drain_file(self) -> None:
        chunk = await asyncio.to_thread(self._read_new_bytes)
        if not chunk:
            await self._maybe_persist_cursor()
            return
        data = self._partial + chunk
        lines = data.split(b"\n")
        self._partial = lines[-1]
        for raw in lines[:-1]:
            if not raw:
                continue
            await self._handle_line(raw)
            if self._stop_requested:
                break
        await self._maybe_persist_cursor()

    def _read_new_bytes(self) -> bytes:
        if not self._path.exists():
            return b""
        try:
            with self._path.open("rb") as fh:
                fh.seek(self._offset)
                data = fh.read()
        except FileNotFoundError:
            return b""
        self._offset += len(data)
        return data

    async def _handle_line(self, raw: bytes) -> None:
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError:
            self._events_dropped_malformed += 1
            logger.warning("non-utf8 transcript line dropped (len=%d)", len(raw))
            return

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            self._events_dropped_malformed += 1
            logger.warning("malformed transcript line: %r", line[:200])
            return

        if not isinstance(event, dict):
            self._events_dropped_malformed += 1
            logger.warning("non-object transcript line: %r", line[:200])
            return

        evt_type = event.get("type")
        if evt_type not in KNOWN_EVENT_TYPES:
            self._events_dropped_unknown += 1
            logger.debug("unknown event type %r dropped", evt_type)
            return

        if not self._past_resume_cursor:
            elapsed = event.get("elapsed", 0)
            try:
                elapsed_f = float(elapsed)
            except (TypeError, ValueError):
                elapsed_f = 0.0
            if elapsed_f <= (self._start_after_elapsed or 0.0):
                return
            self._past_resume_cursor = True

        await self._put_event(event)

    async def _put_event(self, event: dict[str, Any]) -> None:
        if self._queue.full():
            # Drop-oldest under contention may rarely drop two items; that's
            # acceptable per design (forward-progress over strict accounting).
            try:
                self._queue.get_nowait()
                self._events_dropped_queue_full += 1
            except asyncio.QueueEmpty:
                pass
        await self._queue.put(event)
        self._events_emitted += 1
        elapsed = event.get("elapsed")
        if isinstance(elapsed, int | float):
            self._last_emitted_elapsed = float(elapsed)
        self._events_since_persist += 1

    async def _maybe_persist_cursor(self, *, force: bool = False) -> None:
        if self._session_dir is None or self._last_emitted_elapsed is None:
            return
        if self._last_emitted_elapsed == self._last_persisted_elapsed and not force:
            return
        elapsed_due = (
            time.monotonic() - self._last_persist_ts
        ) >= self._persist_every_seconds
        count_due = self._events_since_persist >= self._persist_every_events
        if not (elapsed_due or count_due or force):
            return
        try:
            await asyncio.to_thread(
                state.set_config,
                self._session_dir,
                "tailer.last_event_elapsed",
                self._last_emitted_elapsed,
            )
        except Exception:
            logger.debug("cursor persist failed", exc_info=True)
            return
        self._last_persisted_elapsed = self._last_emitted_elapsed
        self._last_persist_ts = time.monotonic()
        self._events_since_persist = 0
