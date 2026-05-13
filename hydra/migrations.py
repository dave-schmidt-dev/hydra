"""Numbered, transactional schema migrations for the session state DB."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

_V1_SCHEMA_SQL = """
CREATE TABLE questions (
  id              INTEGER PRIMARY KEY,
  q_id            TEXT UNIQUE NOT NULL,
  status          TEXT NOT NULL,
  source          TEXT NOT NULL,
  topic           TEXT NOT NULL,
  rationale       TEXT,
  confidence      REAL,
  transcript_window TEXT,
  created_at      TEXT NOT NULL,
  promoted_at     TEXT,
  user_notes      TEXT,
  in_report       INTEGER DEFAULT 1
);

CREATE TABLE research_jobs (
  id              INTEGER PRIMARY KEY,
  q_id            TEXT NOT NULL REFERENCES questions(q_id),
  tier            TEXT NOT NULL,
  model_spec      TEXT NOT NULL,
  parallel_group  TEXT,
  status          TEXT NOT NULL,
  started_at      TEXT,
  completed_at    TEXT,
  duration_s      REAL,
  tokens_in       INTEGER,
  tokens_out      INTEGER,
  cost_usd        REAL,
  error           TEXT,
  artifact_path   TEXT
);

CREATE TABLE citations (
  id              INTEGER PRIMARY KEY,
  q_id            TEXT NOT NULL REFERENCES questions(q_id),
  source_type     TEXT NOT NULL,
  url             TEXT,
  file_path       TEXT,
  quoted_snippet  TEXT NOT NULL,
  claim           TEXT NOT NULL,
  model_spec      TEXT,
  head_status     INTEGER
);

CREATE TABLE index_progress (
  corpus_root     TEXT PRIMARY KEY,
  total_files     INTEGER,
  indexed_files   INTEGER,
  status          TEXT NOT NULL,
  started_at      TEXT,
  completed_at    TEXT,
  error           TEXT
);

CREATE TABLE config (
  key             TEXT PRIMARY KEY,
  value           TEXT NOT NULL
);
"""


def _migration_v1(conn: sqlite3.Connection) -> None:
    for stmt in filter(None, (s.strip() for s in _V1_SCHEMA_SQL.split(";"))):
        conn.execute(stmt)


def _migration_v2(conn: sqlite3.Connection) -> None:
    # v2 marker column — exists so the migration-mechanism is itself tested end-to-end
    conn.execute("ALTER TABLE questions ADD COLUMN _v2_marker TEXT DEFAULT NULL")


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _migration_v1,
    _migration_v2,
]


def apply_migrations(conn: sqlite3.Connection, target_version: int) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= target_version:
        return
    conn.execute("BEGIN")
    try:
        for version in range(current, target_version):
            migration = MIGRATIONS[version]
            try:
                migration(conn)
            except Exception as exc:
                raise RuntimeError(
                    f"Migration {version + 1} failed; "
                    "restore session-dir backup before retrying"
                ) from exc
            conn.execute(f"PRAGMA user_version = {version + 1}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
