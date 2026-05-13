from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_TAIL_BYTES = 64 * 1024
_SESSION_END_MARKER = b'"session_end"'


@dataclass(frozen=True)
class ProbeResult:
    session_dir: Path
    reason: str


class NoLiveSessionError(RuntimeError):
    """Raised when no live session is discoverable."""


class SessionEndedDuringWaitError(RuntimeError):
    """Raised by find_live_session_blocking when session_end arrives during the wait."""


def _transcript_has_session_end(transcript: Path) -> bool:
    try:
        with transcript.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            offset = max(0, size - _TAIL_BYTES)
            fh.seek(offset)
            tail = fh.read()
    except OSError:
        return False
    return _SESSION_END_MARKER in tail


def is_live_session(
    session_dir: Path,
    *,
    now: float | None = None,
    recent_mtime_window_s: float = 300.0,
) -> tuple[bool, str | None]:
    transcript = session_dir / "transcript.jsonl"
    if not transcript.is_file():
        return (False, None)
    if _transcript_has_session_end(transcript):
        return (False, None)
    audio = session_dir / "audio.wav"
    if audio.is_file():
        return (True, "audio.wav present")
    segments = sorted(session_dir.glob("audio_seg*.wav"))
    if segments:
        return (True, f"post-rotation segment {segments[-1].name}")
    current = time.time() if now is None else now
    try:
        mtime = transcript.stat().st_mtime
    except OSError:
        return (False, None)
    if current - mtime <= recent_mtime_window_s:
        return (True, "transcript mtime recent")
    return (False, None)


def find_live_session(
    recordings_root: Path,
    *,
    explicit: Path | None = None,
    now: float | None = None,
) -> ProbeResult:
    if explicit is not None:
        if not explicit.is_dir():
            raise NoLiveSessionError(f"explicit session not found: {explicit}")
        live, _reason = is_live_session(explicit, now=now)
        if not live:
            raise NoLiveSessionError(f"explicit session is not live: {explicit}")
        return ProbeResult(session_dir=explicit, reason="explicit --session")

    if not recordings_root.is_dir():
        raise NoLiveSessionError(f"recordings root not found: {recordings_root}")

    candidates: list[tuple[float, Path]] = []
    for entry in recordings_root.iterdir():
        if not entry.is_dir():
            continue
        try:
            candidates.append((entry.stat().st_mtime, entry))
        except OSError:
            continue
    candidates.sort(key=lambda pair: pair[0], reverse=True)

    for _mtime, session_dir in candidates:
        live, reason = is_live_session(session_dir, now=now)
        if live and reason is not None:
            return ProbeResult(session_dir=session_dir, reason=reason)

    raise NoLiveSessionError(f"no live session under {recordings_root}")


def find_live_session_blocking(
    recordings_root: Path,
    *,
    wait_seconds: float,
    poll_interval_s: float = 0.5,
    explicit: Path | None = None,
    now_fn: Callable[[], float] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> ProbeResult:
    clock = time.monotonic if now_fn is None else now_fn
    sleeper = time.sleep if sleep_fn is None else sleep_fn
    deadline = clock() + wait_seconds

    while True:
        if explicit is not None:
            transcript = explicit / "transcript.jsonl"
            if (
                explicit.is_dir()
                and transcript.is_file()
                and transcript.stat().st_size > 0
            ):
                if _transcript_has_session_end(transcript):
                    raise SessionEndedDuringWaitError(
                        f"session you were waiting for just ended: {explicit}"
                    )
                live, _reason = is_live_session(explicit)
                if live:
                    return ProbeResult(
                        session_dir=explicit, reason="explicit --session"
                    )
        else:
            try:
                return find_live_session(recordings_root)
            except NoLiveSessionError:
                pass

        if clock() >= deadline:
            raise NoLiveSessionError(
                f"no live session under {recordings_root} after {wait_seconds}s"
            )
        sleeper(poll_interval_s)
