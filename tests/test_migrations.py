"""Tests for the migrations module."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hydra import migrations


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    return conn


def test_fresh_db_migrates_to_v1(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _open(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        migrations.apply_migrations(conn, target_version=1)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "questions",
            "research_jobs",
            "citations",
            "index_progress",
            "config",
        }.issubset(tables)
    finally:
        conn.close()


def test_v1_then_v2_adds_marker_column(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _open(db)
    try:
        migrations.apply_migrations(conn, target_version=1)
        before_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(questions)").fetchall()
        }
        assert "_v2_marker" not in before_cols
        migrations.apply_migrations(conn, target_version=2)
        after_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(questions)").fetchall()
        }
        assert "_v2_marker" in after_cols
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        conn.close()


def test_failed_migration_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "state.db"
    conn = _open(db)
    try:

        def boom(_conn: sqlite3.Connection) -> None:
            raise RuntimeError("intentional failure")

        original_first = migrations.MIGRATIONS[0]
        monkeypatch.setattr(migrations, "MIGRATIONS", [original_first, boom])
        with pytest.raises(RuntimeError, match="Migration"):
            migrations.apply_migrations(conn, target_version=2)
        # user_version stays at 0 because the entire 0->2 transaction rolls back.
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "questions" not in tables
    finally:
        conn.close()


def test_apply_migrations_is_noop_if_at_target(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _open(db)
    try:
        migrations.apply_migrations(conn, target_version=2)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        # Calling again should be a clean no-op.
        migrations.apply_migrations(conn, target_version=2)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        conn.close()


def test_apply_migrations_is_noop_if_past_target(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _open(db)
    try:
        migrations.apply_migrations(conn, target_version=2)
        migrations.apply_migrations(conn, target_version=1)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        conn.close()
