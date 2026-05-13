"""Tests for the audit writer + state-transition mirroring + BUSY retry."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from hydra import audit, state


@pytest.fixture(autouse=True)
def _reset_breaker() -> None:
    state._reset_breaker_for_tests()
    yield
    state._reset_breaker_for_tests()


def _read_jsonl(session_dir: Path) -> list[dict]:
    path = session_dir / "hydra" / "questions.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_emit_creates_jsonl_with_ts(tmp_path: Path) -> None:
    audit.emit(tmp_path, {"event": "flagged", "q_id": "q-001"})
    rows = _read_jsonl(tmp_path)
    assert len(rows) == 1
    assert rows[0]["event"] == "flagged"
    assert rows[0]["q_id"] == "q-001"
    assert "ts" in rows[0]
    # ISO-8601 with timezone; fromisoformat parses both.
    assert "T" in rows[0]["ts"]


def test_emit_appends_does_not_overwrite(tmp_path: Path) -> None:
    audit.emit(tmp_path, {"event": "flagged", "q_id": "q-001"})
    audit.emit(tmp_path, {"event": "dismissed", "q_id": "q-001"})
    rows = _read_jsonl(tmp_path)
    assert [r["event"] for r in rows] == ["flagged", "dismissed"]


def test_emit_strips_none_values(tmp_path: Path) -> None:
    audit.emit(
        tmp_path,
        {"event": "flagged", "q_id": "q-001", "confidence": None, "topic": "x"},
    )
    rows = _read_jsonl(tmp_path)
    assert "confidence" not in rows[0]
    assert rows[0]["topic"] == "x"


def test_state_transitions_mirror_to_jsonl(tmp_path: Path) -> None:
    state.init_session_db(tmp_path)
    for i in range(3):
        state.insert_question(
            tmp_path,
            q_id=f"q-{i:03d}",
            status="pending",
            source="heuristic",
            topic=f"topic-{i}",
            confidence=0.5 + i * 0.1,
        )
    conn = state.open_session_db(tmp_path)
    try:
        db_rows = conn.execute(
            "SELECT q_id, status FROM questions ORDER BY q_id"
        ).fetchall()
    finally:
        conn.close()
    assert [r["q_id"] for r in db_rows] == ["q-000", "q-001", "q-002"]

    audit_rows = _read_jsonl(tmp_path)
    assert len(audit_rows) == 3
    assert [r["q_id"] for r in audit_rows] == ["q-000", "q-001", "q-002"]
    assert all(r["event"] == "flagged" for r in audit_rows)

    state.set_question_status(tmp_path, q_id="q-001", status="dismissed")
    audit_rows = _read_jsonl(tmp_path)
    assert len(audit_rows) == 4
    assert audit_rows[-1]["event"] == "dismissed"
    assert audit_rows[-1]["q_id"] == "q-001"

    conn = state.open_session_db(tmp_path)
    try:
        row = conn.execute(
            "SELECT status FROM questions WHERE q_id = 'q-001'"
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "dismissed"


def test_set_question_status_with_extras(tmp_path: Path) -> None:
    state.init_session_db(tmp_path)
    state.insert_question(
        tmp_path, q_id="q-1", status="pending", source="manual", topic="t"
    )
    state.set_question_status(
        tmp_path, q_id="q-1", status="answered", user_notes="ok", in_report=0
    )
    conn = state.open_session_db(tmp_path)
    try:
        row = conn.execute(
            "SELECT status, user_notes, in_report FROM questions WHERE q_id = 'q-1'"
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "answered"
    assert row["user_notes"] == "ok"
    assert row["in_report"] == 0
    audit_rows = _read_jsonl(tmp_path)
    assert audit_rows[-1]["event"] == "answered"
    assert audit_rows[-1]["user_notes"] == "ok"
    assert audit_rows[-1]["in_report"] == 0


class _FlakyConnection:
    """Real connection wrapper that injects BUSY errors into execute()."""

    def __init__(self, real: sqlite3.Connection, fail_times: int) -> None:
        self._real = real
        self._fail_times = fail_times
        self.attempts = 0

    def execute(self, *args, **kwargs):
        self.attempts += 1
        if self.attempts <= self._fail_times:
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def close(self) -> None:
        self._real.close()


def test_busy_retry_recovers_after_transient_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state.init_session_db(tmp_path)

    call_count = {"n": 0}
    original_open = state.open_session_db

    def patched_open(session_dir: Path) -> sqlite3.Connection:
        real = original_open(session_dir)
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _FlakyConnection(real, fail_times=2)
        return real

    monkeypatch.setattr(state, "open_session_db", patched_open)
    # Should succeed after retries.
    state.insert_question(
        tmp_path, q_id="q-retry", status="pending", source="heuristic", topic="t"
    )
    conn = sqlite3.connect(tmp_path / "hydra" / "state.db")
    try:
        row = conn.execute("SELECT q_id FROM questions WHERE q_id='q-retry'").fetchone()
    finally:
        conn.close()
    assert row is not None


def test_circuit_breaker_trips_after_exhausted_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state.init_session_db(tmp_path)

    sql_calls = {"n": 0}
    original_open = state.open_session_db

    def always_busy_open(session_dir: Path) -> sqlite3.Connection:
        real = original_open(session_dir)
        return _CountingAlwaysBusyConnection(real, sql_calls)

    monkeypatch.setattr(state, "open_session_db", always_busy_open)

    with pytest.raises(state.StateStoreCircuitBreakerTripped):
        state.insert_question(
            tmp_path, q_id="q-fail", status="pending", source="heuristic", topic="t"
        )
    first_attempts = sql_calls["n"]
    assert first_attempts == 5  # 5 attempts total

    # Breaker should now be open; next call must raise immediately without SQL.
    with pytest.raises(state.StateStoreCircuitBreakerTripped):
        state.insert_question(
            tmp_path,
            q_id="q-fail-2",
            status="pending",
            source="heuristic",
            topic="t",
        )
    assert sql_calls["n"] == first_attempts  # no new SQL attempts


def test_breaker_resets_after_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state.init_session_db(tmp_path)
    monkeypatch.setattr(state, "CIRCUIT_BREAKER_SECONDS", 0.05)

    sql_calls = {"n": 0}
    original_open = state.open_session_db
    fail_mode = {"on": True}

    def conditional_open(session_dir: Path) -> sqlite3.Connection:
        real = original_open(session_dir)
        if fail_mode["on"]:
            return _CountingAlwaysBusyConnection(real, sql_calls)
        return real

    monkeypatch.setattr(state, "open_session_db", conditional_open)

    with pytest.raises(state.StateStoreCircuitBreakerTripped):
        state.insert_question(
            tmp_path, q_id="q-x", status="pending", source="heuristic", topic="t"
        )

    fail_mode["on"] = False
    time.sleep(0.1)
    # After the breaker window elapsed, this must succeed.
    state.insert_question(
        tmp_path, q_id="q-after", status="pending", source="heuristic", topic="t"
    )
    conn = sqlite3.connect(tmp_path / "hydra" / "state.db")
    try:
        row = conn.execute("SELECT q_id FROM questions WHERE q_id='q-after'").fetchone()
    finally:
        conn.close()
    assert row is not None


class _CountingAlwaysBusyConnection:
    """Always raises BUSY on execute; counts attempts via shared dict."""

    def __init__(self, real: sqlite3.Connection, counter: dict) -> None:
        self._real = real
        self._counter = counter

    def execute(self, *args, **kwargs):
        self._counter["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    def __getattr__(self, name):
        return getattr(self._real, name)

    def close(self) -> None:
        self._real.close()
