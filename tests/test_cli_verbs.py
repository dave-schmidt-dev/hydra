"""Tests for the CLI argparse skeleton (Task 1.6)."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(*args: str, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hydra", *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=timeout,
        check=False,
    )


def test_help_runs() -> None:
    proc = _run("--help")
    assert proc.returncode == 0
    assert "hydra start" in proc.stdout


def test_start_help() -> None:
    proc = _run("start", "--help")
    assert proc.returncode == 0
    assert "--session" in proc.stdout


def test_status_help() -> None:
    proc = _run("status", "--help")
    assert proc.returncode == 0


def test_stop_help() -> None:
    proc = _run("stop", "--help")
    assert proc.returncode == 0


def test_report_help() -> None:
    proc = _run("report", "--help")
    assert proc.returncode == 0


def test_finalize_help() -> None:
    proc = _run("finalize", "--help")
    assert proc.returncode == 0


def test_prune_help() -> None:
    proc = _run("prune", "--help")
    assert proc.returncode == 0


def test_status_not_yet_implemented() -> None:
    proc = _run("status")
    assert proc.returncode == 1
    assert "not yet implemented" in proc.stderr.lower()


def test_stop_not_yet_implemented() -> None:
    proc = _run("stop")
    assert proc.returncode == 1
    assert "not yet implemented" in proc.stderr.lower()


def test_report_not_yet_implemented(tmp_path: Path) -> None:
    proc = _run("report", str(tmp_path))
    assert proc.returncode == 1
    assert "not yet implemented" in proc.stderr.lower()


def test_finalize_not_yet_implemented(tmp_path: Path) -> None:
    proc = _run("finalize", str(tmp_path))
    assert proc.returncode == 1
    assert "not yet implemented" in proc.stderr.lower()


def test_prune_not_yet_implemented() -> None:
    proc = _run("prune")
    assert proc.returncode == 1
    assert "not yet implemented" in proc.stderr.lower()


def test_unknown_verb() -> None:
    proc = _run("frobnicate")
    assert proc.returncode != 0


def test_no_verb_errors() -> None:
    proc = _run()
    assert proc.returncode != 0


def _make_live_session(tmp_path: Path) -> Path:
    session = tmp_path / "2026-05-13_12-30-00"
    session.mkdir()
    transcript = session / "transcript.jsonl"
    transcript.write_text('{"type":"transcript","elapsed":1.0,"text":"hello"}\n')
    (session / "audio.wav").write_bytes(b"RIFFfake")
    return session


def test_start_with_explicit_session_fixture(tmp_path: Path) -> None:
    session = _make_live_session(tmp_path)

    proc = subprocess.Popen(
        [sys.executable, "-m", "hydra", "start", "--session", str(session)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        time.sleep(2.5)
        # Append another event while running so we see live tailing.
        with (session / "transcript.jsonl").open("a", encoding="utf-8") as fh:
            fh.write('{"type":"transcript","elapsed":2.0,"text":"world"}\n')
            fh.flush()
        time.sleep(1.0)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            stdout, stderr = proc.communicate(timeout=5.0)

    assert "Attached to session" in stdout, f"stdout={stdout!r} stderr={stderr!r}"
    assert (session / "hydra" / "state.db").exists()


def test_start_no_live_session_explicit_nonexistent(tmp_path: Path) -> None:
    proc = _run("start", "--session", str(tmp_path / "does-not-exist"), timeout=5.0)
    assert proc.returncode != 0


def test_start_recordings_dir_no_live_candidate(tmp_path: Path) -> None:
    empty = tmp_path / "empty-recordings"
    empty.mkdir()
    proc = _run("start", "--recordings-root", str(empty), timeout=5.0)
    assert proc.returncode != 0
    combined = (proc.stdout + proc.stderr).lower()
    assert "no live session" in combined or "not found" in combined
