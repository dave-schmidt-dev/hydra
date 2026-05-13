"""Tests for the recording-integrity autouse fixture (conftest.py).

These tests verify that the autouse fixture:

  1. Allows tests to write under tmp_path freely.
  2. Blocks Hydra production code that writes outside the allowlist.
  3. Allows Hydra production code that writes inside the allowlist.
  4. Honors the ``allow_writes_anywhere`` marker as an opt-out.
  5. Documents the subprocess (fork+exec) limitation in the conftest module.

The negative-path tests drive writes from ``hydra._test_violation_demo``,
a tiny helper module under ``hydra/`` whose calling frame matches the
production-code signature the fixture is designed to flag. Sentinel target
paths are crafted so the write would fail (FileNotFoundError) even if the
fixture failed open, ensuring the test asserts the RIGHT exception class.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hydra import _test_violation_demo, audit
from tests.conftest import RecordingIntegrityViolation


def test_tmp_path_writes_from_test_code_are_allowed(tmp_path: Path) -> None:
    target = tmp_path / "test_write.txt"
    target.write_text("hello")
    assert target.read_text() == "hello"

    target_bytes = tmp_path / "test_write.bin"
    target_bytes.write_bytes(b"hello-bytes")
    assert target_bytes.read_bytes() == b"hello-bytes"

    with (tmp_path / "test_open.txt").open("w") as fh:
        fh.write("via-open")
    assert (tmp_path / "test_open.txt").read_text() == "via-open"


def test_hydra_production_write_inside_allowlist_succeeds(tmp_path: Path) -> None:
    # audit.emit writes <session_dir>/hydra/questions.jsonl.
    # tmp_path is under pytest basetemp → allowlisted for tests.
    audit.emit(tmp_path, {"event": "flagged", "q_id": "q-allow"})
    written = tmp_path / "hydra" / "questions.jsonl"
    assert written.exists()
    assert "q-allow" in written.read_text()


def test_fixture_blocks_production_write_text_outside_allowlist() -> None:
    # Sentinel path: parent does not exist anywhere. Even if the fixture
    # failed open, write_text would raise FileNotFoundError, not the
    # RecordingIntegrityViolation we assert against.
    forbidden = Path("/nonexistent_root_dir_for_hydra_test/forbidden.txt")
    with pytest.raises(RecordingIntegrityViolation):
        _test_violation_demo.write_text_at(forbidden)


def test_fixture_blocks_production_write_bytes_outside_allowlist() -> None:
    forbidden = Path("/nonexistent_root_dir_for_hydra_test/forbidden.bin")
    with pytest.raises(RecordingIntegrityViolation):
        _test_violation_demo.write_bytes_at(forbidden)


def test_fixture_blocks_production_builtins_open_outside_allowlist() -> None:
    forbidden = Path("/nonexistent_root_dir_for_hydra_test/forbidden.open")
    with pytest.raises(RecordingIntegrityViolation):
        _test_violation_demo.open_for_write_at(forbidden)


def test_fixture_allows_production_writes_under_tmp_path(tmp_path: Path) -> None:
    # tmp_path is under pytest basetemp; production code (the demo helper)
    # writing there must succeed.
    target = tmp_path / "ok.txt"
    _test_violation_demo.write_text_at(target, "ok")
    assert target.read_text() == "ok"


@pytest.mark.allow_writes_anywhere
def test_marker_bypasses_fixture(tmp_path: Path) -> None:
    # With the marker, the fixture yields without installing patches at all.
    # Writes from production code paths are not checked. We use tmp_path so
    # the test does not actually leak files outside test scratch.
    target = tmp_path / "marker_bypass.txt"
    _test_violation_demo.write_text_at(target, "bypassed")
    assert target.read_text() == "bypassed"


def test_conftest_documents_subprocess_limitation() -> None:
    # The fixture intercepts only in-process Python writes. Subprocess writes
    # (fork+exec) cannot be intercepted because monkeypatches do not propagate.
    # That contract must be explicit in the conftest module docstring so
    # future maintainers know not to expect subprocess coverage.
    from tests import conftest

    assert conftest.__doc__ is not None
    doc = conftest.__doc__.lower()
    assert "subprocess" in doc
    assert "fork" in doc or "exec" in doc
