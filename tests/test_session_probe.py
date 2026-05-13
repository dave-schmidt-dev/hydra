"""Tests for the live-session probe (Task 1.2)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hydra.probe import (
    NoLiveSessionError,
    ProbeResult,
    SessionEndedDuringWaitError,
    find_live_session,
    find_live_session_blocking,
    is_live_session,
)


def _make_session(
    root: Path,
    name: str,
    *,
    transcript_lines: list[str] | None = None,
    audio_files: list[str] | None = None,
    transcript_mtime: float | None = None,
    dir_mtime: float | None = None,
) -> Path:
    session_dir = root / name
    session_dir.mkdir(parents=True, exist_ok=True)
    transcript = session_dir / "transcript.jsonl"
    if transcript_lines is not None:
        transcript.write_text(
            "\n".join(transcript_lines) + ("\n" if transcript_lines else "")
        )
    for audio_name in audio_files or []:
        (session_dir / audio_name).write_bytes(b"RIFFfake")
    if transcript_mtime is not None and transcript.exists():
        os.utime(transcript, (transcript_mtime, transcript_mtime))
    if dir_mtime is not None:
        os.utime(session_dir, (dir_mtime, dir_mtime))
    return session_dir


def test_is_live_session_with_audio_wav(tmp_path: Path) -> None:
    now = time.time()
    session = _make_session(
        tmp_path,
        "2026-05-13_12-30-00",
        transcript_lines=['{"type":"transcript","text":"hi"}'],
        audio_files=["audio.wav"],
        transcript_mtime=now - 10,
    )
    live, reason = is_live_session(session, now=now)
    assert live is True
    assert reason == "audio.wav present"


def test_is_live_session_post_rotation_segment(tmp_path: Path) -> None:
    now = time.time()
    session = _make_session(
        tmp_path,
        "2026-05-13_12-30-00",
        transcript_lines=['{"type":"transcript","text":"hi"}'],
        audio_files=["audio_seg2.wav"],
        transcript_mtime=now - 10,
    )
    live, reason = is_live_session(session, now=now)
    assert live is True
    assert reason is not None
    assert "segment" in reason


def test_is_live_session_recent_mtime_fallback(tmp_path: Path) -> None:
    now = time.time()
    session = _make_session(
        tmp_path,
        "2026-05-13_12-30-00",
        transcript_lines=['{"type":"transcript","text":"hi"}'],
        audio_files=[],
        transcript_mtime=now - 60,
    )
    live, reason = is_live_session(session, now=now)
    assert live is True
    assert reason is not None
    assert "mtime" in reason


def test_is_live_session_stale_mtime_no_audio_abandoned(tmp_path: Path) -> None:
    now = time.time()
    session = _make_session(
        tmp_path,
        "2026-05-13_12-30-00",
        transcript_lines=['{"type":"transcript","text":"hi"}'],
        audio_files=[],
        transcript_mtime=now - 600,
    )
    live, reason = is_live_session(session, now=now)
    assert live is False
    assert reason is None


def test_is_live_session_session_end_overrides_audio(tmp_path: Path) -> None:
    now = time.time()
    session = _make_session(
        tmp_path,
        "2026-05-13_12-30-00",
        transcript_lines=[
            '{"type":"transcript","text":"hi"}',
            '{"type":"transcript","text":"trunca',
            '{"type":"session_end","ts":"2026-05-13T12:55:00Z"}',
        ],
        audio_files=["audio.wav"],
        transcript_mtime=now - 10,
    )
    live, reason = is_live_session(session, now=now)
    assert live is False
    assert reason is None


def test_is_live_session_truncated_final_line_robust(tmp_path: Path) -> None:
    now = time.time()
    session = _make_session(
        tmp_path,
        "2026-05-13_12-30-00",
        transcript_lines=[
            '{"type":"transcript","text":"hello"}',
            '{"type":"transcript","te',
        ],
        audio_files=["audio.wav"],
        transcript_mtime=now - 10,
    )
    live, reason = is_live_session(session, now=now)
    assert live is True
    assert reason == "audio.wav present"


def test_is_live_session_no_transcript(tmp_path: Path) -> None:
    session = tmp_path / "2026-05-13_12-30-00"
    session.mkdir()
    (session / "audio.wav").write_bytes(b"RIFFfake")
    live, reason = is_live_session(session, now=time.time())
    assert live is False
    assert reason is None


def test_find_live_session_picks_most_recent(tmp_path: Path) -> None:
    now = time.time()
    _make_session(
        tmp_path,
        "2026-05-13_10-00-00",
        transcript_lines=['{"type":"transcript"}', '{"type":"session_end"}'],
        audio_files=["audio.wav"],
        transcript_mtime=now - 7200,
        dir_mtime=now - 7200,
    )
    _make_session(
        tmp_path,
        "2026-05-13_11-00-00",
        transcript_lines=['{"type":"transcript"}'],
        audio_files=[],
        transcript_mtime=now - 3600,
        dir_mtime=now - 3600,
    )
    live = _make_session(
        tmp_path,
        "2026-05-13_12-30-00",
        transcript_lines=['{"type":"transcript"}'],
        audio_files=["audio.wav"],
        transcript_mtime=now - 30,
        dir_mtime=now - 30,
    )

    result = find_live_session(tmp_path, now=now)
    assert isinstance(result, ProbeResult)
    assert result.session_dir == live
    assert result.reason == "audio.wav present"


def test_find_live_session_raises_when_none_live(tmp_path: Path) -> None:
    now = time.time()
    _make_session(
        tmp_path,
        "2026-05-13_10-00-00",
        transcript_lines=['{"type":"transcript"}', '{"type":"session_end"}'],
        audio_files=["audio.wav"],
        transcript_mtime=now - 7200,
        dir_mtime=now - 7200,
    )
    _make_session(
        tmp_path,
        "2026-05-13_11-00-00",
        transcript_lines=['{"type":"transcript"}'],
        audio_files=[],
        transcript_mtime=now - 7200,
        dir_mtime=now - 7200,
    )

    with pytest.raises(NoLiveSessionError):
        find_live_session(tmp_path, now=now)


def test_find_live_session_explicit_live(tmp_path: Path) -> None:
    now = time.time()
    session = _make_session(
        tmp_path,
        "2026-05-13_12-30-00",
        transcript_lines=['{"type":"transcript"}'],
        audio_files=["audio.wav"],
        transcript_mtime=now - 10,
    )
    result = find_live_session(tmp_path, explicit=session, now=now)
    assert result.session_dir == session
    assert result.reason == "explicit --session"


def test_find_live_session_explicit_not_live(tmp_path: Path) -> None:
    now = time.time()
    session = _make_session(
        tmp_path,
        "2026-05-13_12-30-00",
        transcript_lines=['{"type":"transcript"}', '{"type":"session_end"}'],
        audio_files=["audio.wav"],
        transcript_mtime=now - 10,
    )
    with pytest.raises(NoLiveSessionError):
        find_live_session(tmp_path, explicit=session, now=now)


def test_find_live_session_explicit_nonexistent(tmp_path: Path) -> None:
    with pytest.raises(NoLiveSessionError):
        find_live_session(
            tmp_path,
            explicit=tmp_path / "does-not-exist",
            now=time.time(),
        )


class _VirtualClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def test_find_live_session_blocking_auto_discovery_appears(tmp_path: Path) -> None:
    clock = _VirtualClock()
    ticks = {"n": 0}

    def now_fn() -> float:
        return clock.now()

    def sleep_fn(s: float) -> None:
        clock.sleep(s)
        ticks["n"] += 1
        if ticks["n"] == 2:
            _make_session(
                tmp_path,
                "2026-05-13_12-30-00",
                transcript_lines=['{"type":"transcript"}'],
                audio_files=["audio.wav"],
                transcript_mtime=time.time() - 5,
            )

    result = find_live_session_blocking(
        tmp_path,
        wait_seconds=10.0,
        poll_interval_s=0.5,
        now_fn=now_fn,
        sleep_fn=sleep_fn,
    )
    assert result.session_dir.name == "2026-05-13_12-30-00"
    assert clock.t <= 10.0


def test_find_live_session_blocking_auto_discovery_timeout(tmp_path: Path) -> None:
    clock = _VirtualClock()
    with pytest.raises(NoLiveSessionError):
        find_live_session_blocking(
            tmp_path,
            wait_seconds=2.0,
            poll_interval_s=0.5,
            now_fn=clock.now,
            sleep_fn=clock.sleep,
        )
    assert clock.t >= 2.0


def test_find_live_session_blocking_explicit_session_ends_during_wait(
    tmp_path: Path,
) -> None:
    session = tmp_path / "2026-05-13_12-30-00"
    session.mkdir()
    transcript = session / "transcript.jsonl"
    transcript.write_text("")
    (session / "audio.wav").write_bytes(b"RIFFfake")

    clock = _VirtualClock()
    ticks = {"n": 0}

    def now_fn() -> float:
        return clock.now()

    def sleep_fn(s: float) -> None:
        clock.sleep(s)
        ticks["n"] += 1
        if ticks["n"] == 2:
            transcript.write_text(
                '{"type":"transcript","text":"hi"}\n'
                '{"type":"session_end","ts":"2026-05-13T12:55:00Z"}\n'
            )

    with pytest.raises(SessionEndedDuringWaitError):
        find_live_session_blocking(
            tmp_path,
            wait_seconds=10.0,
            poll_interval_s=0.5,
            explicit=session,
            now_fn=now_fn,
            sleep_fn=sleep_fn,
        )
