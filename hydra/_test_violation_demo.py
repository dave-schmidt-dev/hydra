"""Test-only helper for the recording-integrity fixture demonstration.

This module exists in ``hydra/`` (not ``tests/``) so that the autouse fixture
sees a calling frame under ``<repo>/hydra/`` when these helpers run. That is
the exact production-code signature the fixture is designed to flag.

DO NOT call these helpers from production code paths. They are unguarded
write operations whose sole purpose is to drive the fixture's negative path
in ``tests/test_recording_integrity_fixture.py``.
"""

from __future__ import annotations

from pathlib import Path


def write_text_at(target: Path, payload: str = "forbidden") -> None:
    target.write_text(payload)


def write_bytes_at(target: Path, payload: bytes = b"forbidden") -> None:
    target.write_bytes(payload)


def open_for_write_at(target: Path, payload: str = "forbidden") -> None:
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(payload)
