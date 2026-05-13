"""Process-group-aware subprocess runner with shutdown cleanup.

Every child is spawned with ``process_group=0`` so it becomes the leader of a
fresh POSIX process group. We keep a thread-safe registry of live PGIDs; on
SIGINT/SIGTERM/atexit we SIGTERM each pgid, wait a grace window, then SIGKILL
survivors. Children are isolated from Hydra's own process group, so killpg
cannot take down Hydra itself.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import os
import signal
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class RegisteredProcess:
    pgid: int
    pid: int
    label: str
    started_at: float


_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[int, RegisteredProcess] = {}
_HANDLERS_INSTALLED = False


async def spawn(
    argv: list[str],
    *,
    label: str,
    stdin: int | None = None,
    stdout: int | None = asyncio.subprocess.PIPE,
    stderr: int | None = asyncio.subprocess.PIPE,
    env: dict[str, str] | None = None,
    cwd: str | os.PathLike | None = None,
) -> asyncio.subprocess.Process:
    """Spawn ``argv`` in a new process group and register its PGID."""
    install_handlers_once()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        env=env,
        cwd=cwd,
        process_group=0,
    )
    pgid = os.getpgid(proc.pid)
    with _REGISTRY_LOCK:
        _REGISTRY[pgid] = RegisteredProcess(
            pgid=pgid,
            pid=proc.pid,
            label=label,
            started_at=time.monotonic(),
        )
    return proc


def release(proc: asyncio.subprocess.Process) -> None:
    """Remove ``proc`` from the registry after it has exited."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        with _REGISTRY_LOCK:
            for key, rp in list(_REGISTRY.items()):
                if rp.pid == proc.pid:
                    del _REGISTRY[key]
                    return
        return
    with _REGISTRY_LOCK:
        _REGISTRY.pop(pgid, None)


def snapshot() -> list[RegisteredProcess]:
    """Return a copy of the current registry contents (diagnostic accessor)."""
    with _REGISTRY_LOCK:
        return list(_REGISTRY.values())


def cleanup_all(grace_seconds: float = 2.0) -> None:
    """SIGTERM all registered pgids, wait grace_seconds, SIGKILL survivors."""
    with _REGISTRY_LOCK:
        snapshot_list = list(_REGISTRY.values())
        _REGISTRY.clear()
    for rp in snapshot_list:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(rp.pgid, signal.SIGTERM)
    if not snapshot_list:
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        alive = [rp for rp in snapshot_list if _pgid_alive(rp.pgid)]
        if not alive:
            return
        time.sleep(0.05)
    for rp in snapshot_list:
        if _pgid_alive(rp.pgid):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(rp.pgid, signal.SIGKILL)


def _pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but owned by another user — should never occur under
        # our spawn model; treat as alive so we don't silently leak.
        return True


def install_handlers_once() -> None:
    """Install SIGINT/SIGTERM + atexit cleanup handlers. Safe to call repeatedly."""
    global _HANDLERS_INSTALLED
    if _HANDLERS_INSTALLED:
        return
    _HANDLERS_INSTALLED = True
    prev_int = signal.getsignal(signal.SIGINT)
    prev_term = signal.getsignal(signal.SIGTERM)

    def _handler(signum, frame):
        cleanup_all()
        prev = prev_int if signum == signal.SIGINT else prev_term
        if callable(prev):
            prev(signum, frame)
        elif prev == signal.SIG_DFL:
            if signum == signal.SIGINT:
                raise KeyboardInterrupt
            raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)
    except ValueError:
        # signal.signal() raises ValueError off the main thread (e.g. under
        # pytest-asyncio's worker threads). atexit still fires for normal exits.
        pass

    atexit.register(cleanup_all)


def _reset_for_tests() -> None:
    """Test hook: clear the registry and the handler-install latch."""
    global _HANDLERS_INSTALLED
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
    _HANDLERS_INSTALLED = False
