"""SQLite FTS5 corpus indexer with incremental mtime caching.

Walks corpus paths via ``os.walk(followlinks=False)``, skips binary
mimetypes / oversize files / well-known cache directories, and emits
progress events. The persistent DB lives at
``~/Documents/Projects/hydra/.cache/index.sqlite`` and is regeneratable.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("hydra.indexer")

DEFAULT_INDEX_PATH = (
    Path.home() / "Documents" / "Projects" / "hydra" / ".cache" / "index.sqlite"
)

MAX_FILE_BYTES = 5 * 1024 * 1024

SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "__pycache__",
        ".obsidian",
        ".DS_Store",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
    }
)

SKIP_FILE_NAMES: frozenset[str] = frozenset({".DS_Store"})

_PROGRESS_EVERY = 20

_SCHEMA_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS files USING fts5(
    corpus_root UNINDEXED,
    path UNINDEXED,
    content,
    tokenize='unicode61'
);
CREATE TABLE IF NOT EXISTS file_meta (
    corpus_root TEXT NOT NULL,
    path TEXT NOT NULL,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    indexed_at TEXT NOT NULL,
    PRIMARY KEY (corpus_root, path)
);
"""


@dataclass
class IndexerProgress:
    """Snapshot of a single corpus root's indexing state."""

    corpus_root: Path
    total_files: int = 0
    indexed_files: int = 0
    skipped_files: int = 0
    status: str = "pending"
    started_at: float | None = None
    completed_at: float | None = None
    error: str | None = None


IndexerEventSink = Callable[[str, dict], Awaitable[None]] | None


class Indexer:
    def __init__(
        self,
        *,
        db_path: Path = DEFAULT_INDEX_PATH,
        max_file_bytes: int = MAX_FILE_BYTES,
        on_event: IndexerEventSink = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._max_file_bytes = max_file_bytes
        self._on_event = on_event
        self._clock = clock or time.monotonic
        self._db_lock = threading.Lock()

    # -- Schema setup --

    def init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._open_conn()
        try:
            for stmt in filter(None, (s.strip() for s in _SCHEMA_SQL.split(";"))):
                conn.execute(stmt)
        finally:
            conn.close()

    def _open_conn(self) -> sqlite3.Connection:
        # check_same_thread=False because async tasks dispatch DB work to the
        # default executor; we serialize access via self._db_lock instead.
        conn = sqlite3.connect(
            self._db_path, isolation_level=None, check_same_thread=False
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # -- Walking / indexing --

    async def index(self, corpus_roots: Iterable[Path]) -> dict[Path, IndexerProgress]:
        roots = list(corpus_roots)
        self.init_db()
        conn = self._open_conn()
        try:
            results = await asyncio.gather(
                *[self._index_one_root(conn, r) for r in roots]
            )
        finally:
            conn.close()
        return dict(zip(roots, results, strict=True))

    async def _index_one_root(
        self, conn: sqlite3.Connection, corpus_root: Path
    ) -> IndexerProgress:
        progress = IndexerProgress(corpus_root=corpus_root)
        progress.started_at = self._clock()

        if not corpus_root.exists():
            progress.status = "error"
            progress.error = "missing"
            progress.completed_at = self._clock()
            await self._emit(
                "index_status",
                {
                    "corpus_root": str(corpus_root),
                    "status": "error",
                    "error": "missing",
                },
            )
            return progress

        progress.status = "running"
        await self._emit(
            "index_status",
            {"corpus_root": str(corpus_root), "status": "running", "error": None},
        )

        root_key = str(corpus_root.resolve())
        existing = await asyncio.to_thread(self._load_meta, conn, root_key)
        seen: set[str] = set()

        paths = await asyncio.to_thread(lambda: list(self.walk_root(corpus_root)))
        progress.total_files = len(paths)

        for idx, path in enumerate(paths, start=1):
            path_key = str(path.resolve())
            seen.add(path_key)

            skip, reason = await asyncio.to_thread(self.should_skip_file, path)
            if skip:
                progress.skipped_files += 1
                logger.debug("indexer skipped %s: %s", path, reason)
            else:
                try:
                    stat = path.stat()
                except OSError as exc:
                    progress.skipped_files += 1
                    logger.debug("indexer stat failed %s: %s", path, exc)
                    if idx % _PROGRESS_EVERY == 0:
                        await self._emit_progress(corpus_root, progress)
                    continue

                cached = existing.get(path_key)
                if cached is not None and cached == (stat.st_mtime, stat.st_size):
                    pass
                else:
                    indexed = await asyncio.to_thread(
                        self._index_file_sync, conn, root_key, path, stat
                    )
                    if indexed:
                        progress.indexed_files += 1
                    else:
                        progress.skipped_files += 1

            if idx % _PROGRESS_EVERY == 0:
                await self._emit_progress(corpus_root, progress)

        stale = set(existing.keys()) - seen
        if stale:
            await asyncio.to_thread(self._delete_paths, conn, root_key, stale)

        progress.status = "ready"
        progress.completed_at = self._clock()
        await self._emit_progress(corpus_root, progress)
        await self._emit(
            "index_status",
            {"corpus_root": str(corpus_root), "status": "ready", "error": None},
        )
        return progress

    def walk_root(self, corpus_root: Path) -> Iterable[Path]:
        for dirpath, dirnames, filenames in os.walk(corpus_root, followlinks=False):
            # In-place mutation prunes os.walk's recursion: re-assigning would
            # only rebind the local; os.walk follows the original list object.
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
            for fname in filenames:
                yield Path(dirpath) / fname

    def should_skip_file(self, path: Path) -> tuple[bool, str | None]:
        if path.name in SKIP_FILE_NAMES:
            return True, "skip_file_name"

        mtype, _enc = mimetypes.guess_type(str(path))
        if mtype is not None:
            allowed = (
                mtype.startswith("text/")
                or mtype == "application/json"
                or mtype == "application/xml"
            )
            if not allowed:
                return True, "binary_mimetype"

        if path.is_symlink() and not path.exists():
            return True, "broken_symlink"

        try:
            stat = path.stat()
        except OSError:
            return True, "broken_symlink"

        if stat.st_size > self._max_file_bytes:
            return True, "too_large"

        return False, None

    def _index_file_sync(
        self,
        conn: sqlite3.Connection,
        root_key: str,
        path: Path,
        stat: os.stat_result,
    ) -> bool:
        path_key = str(path.resolve())
        try:
            content = path.read_text(errors="replace")
        except OSError as exc:
            logger.debug("indexer read failed %s: %s", path, exc)
            return False

        now = datetime.now(UTC).isoformat()
        with self._db_lock:
            # FTS5 virtual tables don't support a clean UPSERT; delete-then-insert
            # is the canonical pattern.
            conn.execute(
                "DELETE FROM files WHERE corpus_root = ? AND path = ?",
                (root_key, path_key),
            )
            conn.execute(
                "INSERT INTO files (corpus_root, path, content) VALUES (?, ?, ?)",
                (root_key, path_key, content),
            )
            conn.execute(
                """
                INSERT INTO file_meta (corpus_root, path, mtime, size, indexed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(corpus_root, path) DO UPDATE SET
                    mtime = excluded.mtime,
                    size = excluded.size,
                    indexed_at = excluded.indexed_at
                """,
                (root_key, path_key, stat.st_mtime, stat.st_size, now),
            )
        return True

    def _load_meta(
        self, conn: sqlite3.Connection, root_key: str
    ) -> dict[str, tuple[float, int]]:
        with self._db_lock:
            rows = conn.execute(
                "SELECT path, mtime, size FROM file_meta WHERE corpus_root = ?",
                (root_key,),
            ).fetchall()
        return {row[0]: (row[1], row[2]) for row in rows}

    def _delete_paths(
        self, conn: sqlite3.Connection, root_key: str, paths: set[str]
    ) -> None:
        with self._db_lock:
            for p in paths:
                conn.execute(
                    "DELETE FROM files WHERE corpus_root = ? AND path = ?",
                    (root_key, p),
                )
                conn.execute(
                    "DELETE FROM file_meta WHERE corpus_root = ? AND path = ?",
                    (root_key, p),
                )

    # -- Query --

    def query(self, q: str, *, limit: int = 20) -> list[tuple[Path, Path, float]]:
        conn = self._open_conn()
        try:
            rows = conn.execute(
                """
                SELECT corpus_root, path, bm25(files) AS score
                FROM files
                WHERE files MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (q, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()
        return [(Path(r[0]), Path(r[1]), float(r[2])) for r in rows]

    # -- Inspection --

    def cached_files(self, corpus_root: Path) -> dict[str, float]:
        root_key = str(Path(corpus_root).resolve())
        if not self._db_path.exists():
            return {}
        conn = self._open_conn()
        try:
            rows = conn.execute(
                "SELECT path, mtime FROM file_meta WHERE corpus_root = ?",
                (root_key,),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        finally:
            conn.close()
        return {row[0]: float(row[1]) for row in rows}

    # -- Events --

    async def _emit(self, kind: str, payload: dict) -> None:
        if self._on_event is None:
            return
        await self._on_event(kind, payload)

    async def _emit_progress(
        self, corpus_root: Path, progress: IndexerProgress
    ) -> None:
        await self._emit(
            "index_progress",
            {
                "corpus_root": str(corpus_root),
                "total": progress.total_files,
                "indexed": progress.indexed_files,
                "skipped": progress.skipped_files,
            },
        )
