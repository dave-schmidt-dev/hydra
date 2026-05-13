"""Incremental indexing: mtime cache hits, touched/modified/deleted files."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hydra.indexer import Indexer


@pytest.mark.asyncio
async def test_second_run_with_no_changes_indexes_nothing(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("alpha\n")
    (corpus / "b.md").write_text("bravo\n")

    db = tmp_path / "idx.sqlite"
    indexer = Indexer(db_path=db)
    first = await indexer.index([corpus])
    assert first[corpus].indexed_files == 2

    indexer2 = Indexer(db_path=db)
    second = await indexer2.index([corpus])
    assert second[corpus].indexed_files == 0
    assert second[corpus].total_files == 2
    assert second[corpus].status == "ready"


@pytest.mark.asyncio
async def test_touched_file_is_reindexed(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    a = corpus / "a.md"
    b = corpus / "b.md"
    a.write_text("alpha\n")
    b.write_text("bravo\n")

    db = tmp_path / "idx.sqlite"
    await Indexer(db_path=db).index([corpus])

    # Forcibly advance the mtime of `a` to ensure the indexer detects it,
    # without depending on filesystem mtime resolution.
    st = a.stat()
    os.utime(a, (st.st_atime, st.st_mtime + 5.0))

    result = await Indexer(db_path=db).index([corpus])
    assert result[corpus].indexed_files == 1


@pytest.mark.asyncio
async def test_modified_content_is_queryable(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    note = corpus / "note.md"
    note.write_text("original alpha text\n")

    db = tmp_path / "idx.sqlite"
    await Indexer(db_path=db).index([corpus])

    note.write_text("updated zephyr content\n")
    st = note.stat()
    os.utime(note, (st.st_atime, st.st_mtime + 5.0))

    indexer = Indexer(db_path=db)
    await indexer.index([corpus])

    assert indexer.query("zephyr")
    assert not indexer.query("alpha")


@pytest.mark.asyncio
async def test_deleted_file_removed_from_index(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    keep = corpus / "keep.md"
    drop = corpus / "drop.md"
    keep.write_text("keep me alpha\n")
    drop.write_text("delete me bravo\n")

    db = tmp_path / "idx.sqlite"
    indexer = Indexer(db_path=db)
    await indexer.index([corpus])
    assert indexer.query("bravo")

    drop.unlink()
    indexer2 = Indexer(db_path=db)
    await indexer2.index([corpus])

    assert not indexer2.query("bravo")
    assert indexer2.query("alpha")


@pytest.mark.asyncio
async def test_cached_files_reflects_disk_state(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    a = corpus / "a.md"
    b = corpus / "b.md"
    a.write_text("alpha\n")
    b.write_text("bravo\n")

    db = tmp_path / "idx.sqlite"
    indexer = Indexer(db_path=db)
    await indexer.index([corpus])

    cached = indexer.cached_files(corpus)
    assert set(cached.keys()) == {
        str(a.resolve()),
        str(b.resolve()),
    }
    for path_str, mtime in cached.items():
        assert isinstance(mtime, float)
        assert mtime == pytest.approx(Path(path_str).stat().st_mtime, rel=1e-6)
