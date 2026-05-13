"""SQLite session state store with BUSY-retry + circuit-breaker."""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hydra import audit, migrations

CIRCUIT_BREAKER_SECONDS = 30.0
_RETRY_DELAYS_MS = (10, 20, 40, 80, 160)

_breaker_lock = threading.Lock()
_breaker_until_ts = 0.0


class StateStoreCircuitBreakerTripped(RuntimeError):  # noqa: N818
    pass


def _reset_breaker_for_tests() -> None:
    global _breaker_until_ts
    with _breaker_lock:
        _breaker_until_ts = 0.0


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _breaker_open() -> bool:
    with _breaker_lock:
        return time.monotonic() < _breaker_until_ts


def _trip_breaker() -> None:
    global _breaker_until_ts
    with _breaker_lock:
        _breaker_until_ts = time.monotonic() + CIRCUIT_BREAKER_SECONDS


def with_busy_retry(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if _breaker_open():
            raise StateStoreCircuitBreakerTripped("State store circuit breaker is open")
        last_exc: sqlite3.OperationalError | None = None
        for attempt, delay_ms in enumerate(_RETRY_DELAYS_MS):
            try:
                return fn(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not _is_busy_error(exc):
                    raise
                last_exc = exc
                if attempt < len(_RETRY_DELAYS_MS) - 1:
                    time.sleep(delay_ms / 1000.0)
        _trip_breaker()
        raise StateStoreCircuitBreakerTripped(
            "State store BUSY retries exhausted; circuit breaker tripped"
        ) from last_exc

    return wrapper


def open_session_db(session_dir: Path) -> sqlite3.Connection:
    hydra_dir = session_dir / "hydra"
    hydra_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(hydra_dir / "state.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=0")
    return conn


def init_session_db(session_dir: Path) -> None:
    conn = open_session_db(session_dir)
    try:
        migrations.apply_migrations(conn, target_version=len(migrations.MIGRATIONS))
    finally:
        conn.close()


@with_busy_retry
def insert_question(
    session_dir: Path,
    *,
    q_id: str,
    status: str,
    source: str,
    topic: str,
    rationale: str | None = None,
    confidence: float | None = None,
    transcript_window: str | None = None,
) -> None:
    created_at = datetime.now(UTC).isoformat()
    conn = open_session_db(session_dir)
    try:
        conn.execute(
            """
            INSERT INTO questions (
                q_id, status, source, topic, rationale, confidence,
                transcript_window, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                q_id,
                status,
                source,
                topic,
                rationale,
                confidence,
                transcript_window,
                created_at,
            ),
        )
    finally:
        conn.close()
    event = "flagged" if status in ("pending", "suggested") else status
    audit.emit(
        session_dir,
        {
            "event": event,
            "q_id": q_id,
            "source": source,
            "topic": topic,
            "confidence": confidence,
        },
    )


@with_busy_retry
def set_question_status(
    session_dir: Path,
    *,
    q_id: str,
    status: str,
    user_notes: str | None = None,
    in_report: int | None = None,
) -> None:
    sets = ["status = ?"]
    params: list[object] = [status]
    if user_notes is not None:
        sets.append("user_notes = ?")
        params.append(user_notes)
    if in_report is not None:
        sets.append("in_report = ?")
        params.append(in_report)
    params.append(q_id)
    conn = open_session_db(session_dir)
    try:
        conn.execute(
            f"UPDATE questions SET {', '.join(sets)} WHERE q_id = ?",
            tuple(params),
        )
    finally:
        conn.close()
    audit.emit(
        session_dir,
        {
            "event": status,
            "q_id": q_id,
            "user_notes": user_notes,
            "in_report": in_report,
        },
    )


@with_busy_retry
def set_config(session_dir: Path, key: str, value: Any) -> None:
    conn = open_session_db(session_dir)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )
    finally:
        conn.close()


@with_busy_retry
def get_config(session_dir: Path, key: str, default: Any = None) -> Any:
    conn = open_session_db(session_dir)
    try:
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return default
    return json.loads(row[0])
