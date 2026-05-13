"""Session-end probe robustness tests (CV-7).

Exercises hydra.probe._transcript_has_session_end and is_live_session against
edge cases: truncated lines, malformed session_end, deep-in-file markers,
and the 64KB tail-read boundary.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from hydra.probe import _transcript_has_session_end, is_live_session


def _write_transcript(path: Path, lines: list[str]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line)
            if not line.endswith("\n"):
                fh.write("\n")


def test_abandoned_session_detected_as_not_live(tmp_path: Path) -> None:
    session = tmp_path / "abandoned"
    session.mkdir()
    transcript = session / "transcript.jsonl"
    _write_transcript(transcript, ['{"type":"transcript","elapsed":1.0,"text":"a"}'])
    now = time.time()
    os.utime(transcript, (now - 600, now - 600))

    live, reason = is_live_session(session, now=now)
    assert live is False
    assert reason is None


def test_truncated_final_line_still_live_via_audio(tmp_path: Path) -> None:
    session = tmp_path / "truncated"
    session.mkdir()
    transcript = session / "transcript.jsonl"
    with transcript.open("w", encoding="utf-8") as fh:
        fh.write('{"type":"transcript","elapsed":1.0,"text":"hello"}\n')
        fh.write('{"type":"transcript","te')  # truncated mid-line
    (session / "audio.wav").write_bytes(b"RIFFfake")
    now = time.time()
    os.utime(transcript, (now - 5, now - 5))

    live, reason = is_live_session(session, now=now)
    assert live is True
    assert reason == "audio.wav present"


def test_malformed_session_end_treated_conservatively(tmp_path: Path) -> None:
    session = tmp_path / "malformed-end"
    session.mkdir()
    transcript = session / "transcript.jsonl"
    with transcript.open("w", encoding="utf-8") as fh:
        fh.write('{"type":"transcript","elapsed":1.0,"text":"hello"}\n')
        fh.write('{"type":"session_end","ts":')  # truncated, no closing brace
    (session / "audio.wav").write_bytes(b"RIFFfake")
    now = time.time()
    os.utime(transcript, (now - 5, now - 5))

    assert _transcript_has_session_end(transcript) is True
    live, reason = is_live_session(session, now=now)
    assert live is False
    assert reason is None


def test_session_end_deep_in_file_is_caught(tmp_path: Path) -> None:
    session = tmp_path / "deep-end"
    session.mkdir()
    transcript = session / "transcript.jsonl"
    with transcript.open("w", encoding="utf-8") as fh:
        i = 0
        while fh.tell() < 80 * 1024:
            fh.write(
                json.dumps(
                    {"type": "transcript", "elapsed": float(i), "text": f"event-{i}"}
                )
                + "\n"
            )
            i += 1
        fh.write('{"type":"session_end","ts":"2026-05-13T12:55:00Z"}\n')
    (session / "audio.wav").write_bytes(b"RIFFfake")
    now = time.time()
    os.utime(transcript, (now - 5, now - 5))

    assert _transcript_has_session_end(transcript) is True
    live, reason = is_live_session(session, now=now)
    assert live is False
    assert reason is None


def test_session_end_just_after_64kb_boundary(tmp_path: Path) -> None:
    """The probe reads the LAST 64KB. Anything in that window must be detected,
    including a session_end placed near the start of that window (i.e. just
    after the file crossed 64KB)."""
    session = tmp_path / "boundary"
    session.mkdir()
    transcript = session / "transcript.jsonl"

    target_pre_end = 64 * 1024
    end_line = '{"type":"session_end","ts":"2026-05-13T12:55:00Z"}\n'
    padding_line = (
        json.dumps({"type": "transcript", "elapsed": 0.0, "text": "x" * 64}) + "\n"
    )

    with transcript.open("w", encoding="utf-8") as fh:
        while fh.tell() + len(padding_line) < target_pre_end:
            fh.write(padding_line)
        fh.write(end_line)
        for _ in range(8):
            fh.write(padding_line)

    (session / "audio.wav").write_bytes(b"RIFFfake")
    now = time.time()
    os.utime(transcript, (now - 5, now - 5))

    assert _transcript_has_session_end(transcript) is True
    live, reason = is_live_session(session, now=now)
    assert live is False
    assert reason is None


def test_no_session_end_below_64kb(tmp_path: Path) -> None:
    session = tmp_path / "small-no-end"
    session.mkdir()
    transcript = session / "transcript.jsonl"
    _write_transcript(
        transcript,
        [
            '{"type":"transcript","elapsed":1.0,"text":"a"}',
            '{"type":"transcript","elapsed":2.0,"text":"b"}',
        ],
    )
    assert _transcript_has_session_end(transcript) is False


def test_session_end_marker_requires_quoted_form(tmp_path: Path) -> None:
    """The marker is b'"session_end"' — bare-substring mentions in prose do not
    match. This narrows false positives to JSON-key-shaped occurrences only."""
    session = tmp_path / "substring"
    session.mkdir()
    transcript = session / "transcript.jsonl"
    _write_transcript(
        transcript,
        ['{"type":"transcript","text":"talked about session_end behavior"}'],
    )
    assert _transcript_has_session_end(transcript) is False


def test_session_end_marker_matches_quoted_key(tmp_path: Path) -> None:
    session = tmp_path / "quoted-key"
    session.mkdir()
    transcript = session / "transcript.jsonl"
    _write_transcript(
        transcript,
        ['{"type":"session_end","ts":"2026-05-13T12:55:00Z"}'],
    )
    assert _transcript_has_session_end(transcript) is True
