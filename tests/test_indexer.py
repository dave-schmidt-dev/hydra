"""Happy-path indexer tests: first-time indexing, FTS5 query, events."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from hydra.indexer import Indexer, IndexerProgress


def _capture_events() -> tuple[
    list[tuple[str, dict]],
    Callable[[str, dict], Awaitable[None]],
]:
    events: list[tuple[str, dict]] = []

    async def on_event(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    return events, on_event


@pytest.mark.asyncio
async def test_first_time_indexing_indexes_all_files(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("alpha bravo charlie\n")
    (corpus / "b.md").write_text("delta echo foxtrot\n")
    (corpus / "c.txt").write_text("golf hotel india\n")

    indexer = Indexer(db_path=tmp_path / "idx.sqlite")
    result = await indexer.index([corpus])

    assert corpus in result
    progress = result[corpus]
    assert isinstance(progress, IndexerProgress)
    assert progress.indexed_files == 3
    assert progress.skipped_files == 0
    assert progress.status == "ready"
    assert progress.total_files == 3


@pytest.mark.asyncio
async def test_query_returns_matching_file_with_bm25_score(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "fox.md").write_text("the quick brown fox jumps over the lazy dog\n")
    (corpus / "lazy.md").write_text("the lazy dog naps in the sun\n")

    indexer = Indexer(db_path=tmp_path / "idx.sqlite")
    await indexer.index([corpus])

    results = indexer.query("fox")
    assert results
    paths = [str(p) for _root, p, _score in results]
    assert any(p.endswith("fox.md") for p in paths)
    # bm25 returns NEGATIVE scores in SQLite (lower = better). We just check
    # the score is a finite float, not a placeholder.
    for _root, _p, score in results:
        assert isinstance(score, float)


@pytest.mark.asyncio
async def test_multi_root_concurrent_indexing(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "one.md").write_text("alpha\n")
    (root_a / "two.md").write_text("bravo\n")
    (root_b / "three.md").write_text("charlie\n")

    indexer = Indexer(db_path=tmp_path / "idx.sqlite")
    result = await indexer.index([root_a, root_b])

    assert result[root_a].indexed_files == 2
    assert result[root_a].status == "ready"
    assert result[root_b].indexed_files == 1
    assert result[root_b].status == "ready"


@pytest.mark.asyncio
async def test_empty_corpus_root_is_ready_with_zero_files(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    indexer = Indexer(db_path=tmp_path / "idx.sqlite")
    result = await indexer.index([empty])

    progress = result[empty]
    assert progress.indexed_files == 0
    assert progress.skipped_files == 0
    assert progress.status == "ready"


@pytest.mark.asyncio
async def test_missing_corpus_root_is_error(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent"

    indexer = Indexer(db_path=tmp_path / "idx.sqlite")
    result = await indexer.index([missing])

    progress = result[missing]
    assert progress.status == "error"
    assert progress.error == "missing"


@pytest.mark.asyncio
async def test_events_emitted_for_each_root(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for i in range(3):
        (corpus / f"f{i}.md").write_text(f"content {i}\n")

    events, on_event = _capture_events()
    indexer = Indexer(db_path=tmp_path / "idx.sqlite", on_event=on_event)
    await indexer.index([corpus])

    kinds = [e[0] for e in events]
    statuses = [e[1].get("status") for e in events if e[0] == "index_status"]
    assert "index_status" in kinds
    assert "running" in statuses
    assert "ready" in statuses
    progress_events = [e for e in events if e[0] == "index_progress"]
    assert progress_events, "expected at least one index_progress event"
    for _kind, payload in progress_events:
        assert "corpus_root" in payload
        assert "total" in payload
        assert "indexed" in payload
        assert "skipped" in payload


@pytest.mark.asyncio
async def test_indexer_persists_across_invocations(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("alpha bravo\n")

    db = tmp_path / "idx.sqlite"
    await Indexer(db_path=db).index([corpus])

    # Second Indexer instance against the SAME db should see the cached file.
    indexer2 = Indexer(db_path=db)
    cached = indexer2.cached_files(corpus)
    assert str((corpus / "a.md").resolve()) in cached
