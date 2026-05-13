"""Schema introspection tests for the session state database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hydra import migrations, state

EXPECTED_QUESTIONS_COLS = {
    "id",
    "q_id",
    "status",
    "source",
    "topic",
    "rationale",
    "confidence",
    "transcript_window",
    "created_at",
    "promoted_at",
    "user_notes",
    "in_report",
}

EXPECTED_RESEARCH_JOBS_COLS = {
    "id",
    "q_id",
    "tier",
    "model_spec",
    "parallel_group",
    "status",
    "started_at",
    "completed_at",
    "duration_s",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "error",
    "artifact_path",
}

EXPECTED_CITATIONS_COLS = {
    "id",
    "q_id",
    "source_type",
    "url",
    "file_path",
    "quoted_snippet",
    "claim",
    "model_spec",
    "head_status",
}

EXPECTED_INDEX_PROGRESS_COLS = {
    "corpus_root",
    "total_files",
    "indexed_files",
    "status",
    "started_at",
    "completed_at",
    "error",
}

EXPECTED_CONFIG_COLS = {"key", "value"}

EXPECTED_TABLES = {
    "questions": EXPECTED_QUESTIONS_COLS,
    "research_jobs": EXPECTED_RESEARCH_JOBS_COLS,
    "citations": EXPECTED_CITATIONS_COLS,
    "index_progress": EXPECTED_INDEX_PROGRESS_COLS,
    "config": EXPECTED_CONFIG_COLS,
}


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    state._reset_breaker_for_tests()
    return tmp_path


def test_init_creates_db_file(session_dir: Path) -> None:
    state.init_session_db(session_dir)
    assert (session_dir / "hydra" / "state.db").exists()


def test_init_sets_wal_mode(session_dir: Path) -> None:
    state.init_session_db(session_dir)
    conn = sqlite3.connect(session_dir / "hydra" / "state.db")
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


def test_init_creates_all_expected_tables(session_dir: Path) -> None:
    state.init_session_db(session_dir)
    conn = sqlite3.connect(session_dir / "hydra" / "state.db")
    try:
        for table, expected_cols in EXPECTED_TABLES.items():
            cols = _column_names(conn, table)
            missing = expected_cols - cols
            assert not missing, f"table {table!r} missing columns: {missing}"
    finally:
        conn.close()


def test_init_sets_user_version_to_full_target(session_dir: Path) -> None:
    state.init_session_db(session_dir)
    conn = sqlite3.connect(session_dir / "hydra" / "state.db")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert version == len(migrations.MIGRATIONS)


def test_foreign_keys_enabled_when_opening(session_dir: Path) -> None:
    state.init_session_db(session_dir)
    conn = state.open_session_db(session_dir)
    try:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    finally:
        conn.close()
    assert fk == 1


def test_init_is_idempotent(session_dir: Path) -> None:
    state.init_session_db(session_dir)
    state.init_session_db(session_dir)
    conn = sqlite3.connect(session_dir / "hydra" / "state.db")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert version == len(migrations.MIGRATIONS)
