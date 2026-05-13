"""Tests for the subprocess runner registry + cleanup machinery."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time

import pytest

from hydra import subprocess_runner


@pytest.fixture(autouse=True)
def _reset_runner_state() -> None:
    subprocess_runner._reset_for_tests()
    yield
    subprocess_runner._reset_for_tests()


async def _spawn_sleeper(seconds: int = 30, label: str = "test:sleeper"):
    return await subprocess_runner.spawn(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        label=label,
    )


async def _spawn_sigterm_ignorer(label: str = "test:resistant"):
    script = (
        "import signal, sys, time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "sys.stdout.write('ready\\n');"
        "sys.stdout.flush();"
        "time.sleep(30)"
    )
    proc = await subprocess_runner.spawn(
        [sys.executable, "-u", "-c", script],
        label=label,
    )
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=5.0)
    assert line.strip() == b"ready"
    return proc


async def _wait_with_timeout(
    proc: asyncio.subprocess.Process, timeout: float
) -> int | None:
    try:
        return await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:
        return None


async def test_spawn_registers_pgid_and_metadata() -> None:
    proc = await _spawn_sleeper(label="test:sleeper-a")
    try:
        snap = subprocess_runner.snapshot()
        assert len(snap) == 1
        entry = snap[0]
        assert entry.pid == proc.pid
        assert entry.label == "test:sleeper-a"
        assert entry.pgid == os.getpgid(proc.pid)
        assert entry.started_at > 0
    finally:
        proc.terminate()
        await _wait_with_timeout(proc, 2.0)


async def test_spawn_puts_child_in_its_own_process_group() -> None:
    proc = await _spawn_sleeper()
    try:
        child_pgid = os.getpgid(proc.pid)
        parent_pgid = os.getpgid(os.getpid())
        assert child_pgid != parent_pgid
        assert child_pgid == proc.pid
    finally:
        proc.terminate()
        await _wait_with_timeout(proc, 2.0)


async def test_release_removes_from_registry() -> None:
    proc = await _spawn_sleeper()
    assert len(subprocess_runner.snapshot()) == 1
    subprocess_runner.release(proc)
    assert subprocess_runner.snapshot() == []
    proc.terminate()
    await _wait_with_timeout(proc, 2.0)


async def test_release_handles_already_reaped_process() -> None:
    proc = await _spawn_sleeper()
    pid = proc.pid
    pgid_before = os.getpgid(pid)
    proc.terminate()
    await proc.wait()
    subprocess_runner.release(proc)
    snap = subprocess_runner.snapshot()
    assert all(rp.pgid != pgid_before for rp in snap)
    assert all(rp.pid != pid for rp in snap)


async def test_cleanup_all_sigterms_well_behaved_children() -> None:
    proc = await _spawn_sleeper()
    subprocess_runner.cleanup_all(grace_seconds=2.0)
    rc = await _wait_with_timeout(proc, 2.0)
    assert rc is not None
    assert proc.returncode is not None
    assert subprocess_runner.snapshot() == []


async def test_cleanup_all_sigkills_resistant_children() -> None:
    proc = await _spawn_sigterm_ignorer()
    start = time.monotonic()
    subprocess_runner.cleanup_all(grace_seconds=0.5)
    rc = await _wait_with_timeout(proc, 3.0)
    elapsed = time.monotonic() - start
    assert rc == -signal.SIGKILL
    assert elapsed >= 0.5
    assert subprocess_runner.snapshot() == []


async def test_cleanup_all_safe_on_empty_registry() -> None:
    subprocess_runner.cleanup_all(grace_seconds=0.1)
    assert subprocess_runner.snapshot() == []


async def test_cleanup_all_is_idempotent() -> None:
    proc = await _spawn_sleeper()
    subprocess_runner.cleanup_all(grace_seconds=1.0)
    subprocess_runner.cleanup_all(grace_seconds=0.1)
    await _wait_with_timeout(proc, 2.0)
    assert subprocess_runner.snapshot() == []


def test_install_handlers_once_is_idempotent() -> None:
    subprocess_runner.install_handlers_once()
    assert subprocess_runner._HANDLERS_INSTALLED is True
    subprocess_runner.install_handlers_once()
    assert subprocess_runner._HANDLERS_INSTALLED is True


async def test_snapshot_returns_copy_not_reference() -> None:
    proc = await _spawn_sleeper()
    try:
        snap = subprocess_runner.snapshot()
        snap.clear()
        assert len(subprocess_runner.snapshot()) == 1
    finally:
        proc.terminate()
        await _wait_with_timeout(proc, 2.0)


async def test_pgid_alive_detects_live_and_dead_processes() -> None:
    proc = await _spawn_sleeper()
    pgid = os.getpgid(proc.pid)
    assert subprocess_runner._pgid_alive(pgid) is True
    proc.terminate()
    await proc.wait()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not subprocess_runner._pgid_alive(pgid):
            break
        await asyncio.sleep(0.05)
    assert subprocess_runner._pgid_alive(pgid) is False


async def test_cleanup_all_tracks_multiple_children() -> None:
    procs = [await _spawn_sleeper(label=f"test:multi-{i}") for i in range(3)]
    assert len(subprocess_runner.snapshot()) == 3
    subprocess_runner.cleanup_all(grace_seconds=2.0)
    for proc in procs:
        await _wait_with_timeout(proc, 2.0)
        assert proc.returncode is not None
    assert subprocess_runner.snapshot() == []
