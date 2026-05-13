"""Skip rules: directory pruning, file-size cap, binary mimetype, symlinks."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hydra.indexer import Indexer


@pytest.mark.asyncio
async def test_git_dir_skipped(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("indexed alpha\n")
    git_dir = corpus / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")

    db = tmp_path / "idx.sqlite"
    indexer = Indexer(db_path=db)
    await indexer.index([corpus])

    assert not indexer.query("refs/heads/main")
    assert indexer.query("alpha")


@pytest.mark.asyncio
async def test_pycache_dir_skipped(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "real.md").write_text("real content alpha\n")
    cache = corpus / "__pycache__"
    cache.mkdir()
    (cache / "foo.pyc").write_text("compiled bravo\n")

    db = tmp_path / "idx.sqlite"
    indexer = Indexer(db_path=db)
    await indexer.index([corpus])

    assert not indexer.query("compiled")
    assert indexer.query("alpha")


@pytest.mark.asyncio
async def test_obsidian_dir_skipped(tmp_path: Path) -> None:
    corpus = tmp_path / "vault"
    corpus.mkdir()
    (corpus / "note.md").write_text("real alpha note\n")
    obs = corpus / ".obsidian"
    obs.mkdir()
    (obs / "workspace.json").write_text('{"workspace":"bravo"}\n')

    db = tmp_path / "idx.sqlite"
    indexer = Indexer(db_path=db)
    await indexer.index([corpus])

    assert not indexer.query("workspace")
    assert indexer.query("alpha")


@pytest.mark.asyncio
async def test_venv_dir_skipped(tmp_path: Path) -> None:
    corpus = tmp_path / "proj"
    corpus.mkdir()
    (corpus / "main.py").write_text("# main alpha\n")
    venv_lib = corpus / ".venv" / "lib" / "site-packages"
    venv_lib.mkdir(parents=True)
    (venv_lib / "x.py").write_text("# venv bravo\n")

    db = tmp_path / "idx.sqlite"
    indexer = Indexer(db_path=db)
    await indexer.index([corpus])

    assert not indexer.query("bravo")
    assert indexer.query("alpha")


@pytest.mark.asyncio
async def test_node_modules_dir_skipped(tmp_path: Path) -> None:
    corpus = tmp_path / "proj"
    corpus.mkdir()
    (corpus / "index.js").write_text("// real alpha\n")
    nm = corpus / "node_modules" / "lib"
    nm.mkdir(parents=True)
    (nm / "pkg.js").write_text("// bravo\n")

    db = tmp_path / "idx.sqlite"
    indexer = Indexer(db_path=db)
    await indexer.index([corpus])

    assert not indexer.query("bravo")
    assert indexer.query("alpha")


@pytest.mark.asyncio
async def test_pytest_ruff_mypy_caches_skipped(tmp_path: Path) -> None:
    corpus = tmp_path / "proj"
    corpus.mkdir()
    (corpus / "main.py").write_text("# alpha\n")
    for cache_name, marker in (
        (".pytest_cache", "bravo"),
        (".ruff_cache", "charlie"),
        (".mypy_cache", "delta"),
    ):
        cdir = corpus / cache_name
        cdir.mkdir()
        (cdir / "blob.txt").write_text(f"cache {marker}\n")

    db = tmp_path / "idx.sqlite"
    indexer = Indexer(db_path=db)
    await indexer.index([corpus])

    for marker in ("bravo", "charlie", "delta"):
        assert not indexer.query(marker), f"{marker} should have been skipped"
    assert indexer.query("alpha")


@pytest.mark.asyncio
async def test_ds_store_skipped(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("indexed alpha\n")
    (corpus / ".DS_Store").write_bytes(b"\x00\x05\x16\x07bravo\x00\x00")

    db = tmp_path / "idx.sqlite"
    indexer = Indexer(db_path=db)
    result = await indexer.index([corpus])

    assert indexer.query("alpha")
    # .DS_Store was encountered but skipped — exactly one indexed.
    assert result[corpus].indexed_files == 1


@pytest.mark.asyncio
async def test_large_file_skipped(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "small.md").write_text("small alpha\n")
    big = corpus / "big.txt"
    big.write_bytes(b"\0" * 6_000_000)

    db = tmp_path / "idx.sqlite"
    indexer = Indexer(db_path=db)
    result = await indexer.index([corpus])

    assert result[corpus].indexed_files == 1
    assert result[corpus].skipped_files >= 1


@pytest.mark.asyncio
async def test_binary_mimetype_skipped(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "note.md").write_text("text alpha\n")
    png = corpus / "image.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 100)

    db = tmp_path / "idx.sqlite"
    indexer = Indexer(db_path=db)
    result = await indexer.index([corpus])

    assert result[corpus].indexed_files == 1
    assert result[corpus].skipped_files >= 1


@pytest.mark.asyncio
async def test_should_skip_file_reasons(tmp_path: Path) -> None:
    """Direct unit-level checks for should_skip_file return reasons."""
    indexer = Indexer(db_path=tmp_path / "idx.sqlite")

    ds = tmp_path / ".DS_Store"
    ds.write_bytes(b"\0")
    skip, reason = indexer.should_skip_file(ds)
    assert skip is True
    assert reason == "skip_file_name"

    png = tmp_path / "image.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    skip, reason = indexer.should_skip_file(png)
    assert skip is True
    assert reason == "binary_mimetype"

    big = tmp_path / "big.txt"
    big.write_bytes(b"\0" * 6_000_000)
    skip, reason = indexer.should_skip_file(big)
    assert skip is True
    assert reason == "too_large"

    broken = tmp_path / "broken_link"
    os.symlink(tmp_path / "does_not_exist", broken)
    skip, reason = indexer.should_skip_file(broken)
    assert skip is True
    assert reason == "broken_symlink"

    ok = tmp_path / "ok.md"
    ok.write_text("hello\n")
    skip, reason = indexer.should_skip_file(ok)
    assert skip is False
    assert reason is None


@pytest.mark.asyncio
async def test_symlink_loop_safe(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("alpha\n")
    loop_dir = corpus / "loop"
    loop_dir.mkdir()
    # Symlink to its own parent — would loop forever if followlinks=True.
    os.symlink(loop_dir, loop_dir / "self")

    db = tmp_path / "idx.sqlite"
    indexer = Indexer(db_path=db)
    result = await indexer.index([corpus])

    assert result[corpus].status == "ready"
    assert indexer.query("alpha")


@pytest.mark.asyncio
async def test_broken_symlink_skipped(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "real.md").write_text("alpha\n")
    os.symlink(corpus / "does_not_exist", corpus / "dangling.md")

    db = tmp_path / "idx.sqlite"
    indexer = Indexer(db_path=db)
    result = await indexer.index([corpus])

    assert result[corpus].status == "ready"
    assert result[corpus].indexed_files == 1
    assert result[corpus].skipped_files >= 1
