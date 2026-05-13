"""Recording-integrity autouse fixture.

Patches ``builtins.open``, ``pathlib.Path.write_text``, ``pathlib.Path.write_bytes``,
and ``os.fsync`` to assert every write originating in Hydra production code
(any frame whose ``__file__`` is under ``<repo>/hydra/``) targets a path inside
the recording-integrity allowlist.

Writes from test code, pytest internals, sqlite, watchdog, and site-packages
pass through unchanged: the fixture filters by *caller frame*, not by
destination, so the only writes it polices are those a Hydra production
module is responsible for.

DESIGN NOTE — subprocess limitation: this is a Python-level intercept.
Subprocess writes are NOT covered. Python monkeypatches do not propagate
across fork+exec to child processes, so ai_monitor's credential writes
(which happen inside its own subprocess) are correctly invisible to this
fixture (per plan pre-mortem PM-2). The fixture catches HYDRA in-process
write violations only.

Opt-out: tests that need wide-open writes (e.g., calibration tooling) may
apply ``@pytest.mark.allow_writes_anywhere`` to bypass the fixture entirely.

Performance: stack inspection on every write adds ~us to ms overhead per call.
The walk uses ``sys._getframe`` and short-circuits at the first hydra/ frame,
keeping the cost bounded to the call depth at the patch site.
"""

from __future__ import annotations

import builtins
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HYDRA_SRC = REPO_ROOT / "hydra"
REPO_CACHE = REPO_ROOT / ".cache"

_TMP_ROOTS = (
    Path("/tmp").resolve(),
    Path("/private/tmp").resolve(),
    Path("/private/var/folders"),
)

_LOG_SENTINELS = (
    "/tmp/hydra.log",
    "/private/tmp/hydra.log",
)


class RecordingIntegrityViolation(AssertionError):  # noqa: N818
    """Raised when Hydra production code writes outside the allowlist.

    Subclasses AssertionError so that pytest treats it as a test failure
    (not an error) — and so existing ``pytest.raises(AssertionError)`` blocks
    in calibration code would still catch it. The N818 ``...Error`` suffix
    convention is waived: this class is a test-time tripwire, not a normal
    runtime exception.
    """


_extra_allowed_roots: list[Path] = []


def _caller_is_hydra_production() -> bool:
    # Walk frames manually instead of inspect.stack(): the latter materializes
    # the full traceback for every frame (slow). sys._getframe + f_back is
    # cheap and we can short-circuit on the first hydra/ frame we find.
    frame = sys._getframe(1)
    hydra_src_str = str(HYDRA_SRC)
    while frame is not None:
        filename = frame.f_code.co_filename
        if filename.startswith(hydra_src_str + os.sep) or filename == hydra_src_str:
            return True
        frame = frame.f_back
    return False


def _is_path_allowed(path: str | os.PathLike, basetemp: Path) -> bool:
    raw = os.fspath(path)
    p = Path(raw)
    p = p.resolve() if p.is_absolute() else (Path.cwd() / p).resolve()
    p_str = str(p)

    for sentinel in _LOG_SENTINELS:
        if p_str == sentinel or p_str.startswith(sentinel):
            return True

    candidates: list[Path] = [basetemp, REPO_CACHE, *_TMP_ROOTS, *_extra_allowed_roots]
    for root in candidates:
        try:
            p.relative_to(root)
            return True
        except ValueError:
            continue

    recordings_root = Path.home() / "recordings"
    try:
        rel = p.relative_to(recordings_root)
        if "hydra" in rel.parts:
            return True
    except ValueError:
        pass

    return False


@pytest.fixture(autouse=True)
def _enforce_recording_integrity(request, monkeypatch, tmp_path_factory):
    if request.node.get_closest_marker("allow_writes_anywhere"):
        yield
        return

    basetemp = tmp_path_factory.getbasetemp().resolve()

    orig_open = builtins.open
    orig_write_text = Path.write_text
    orig_write_bytes = Path.write_bytes
    orig_fsync = os.fsync

    def _check(path) -> None:
        if _caller_is_hydra_production() and not _is_path_allowed(path, basetemp):
            raise RecordingIntegrityViolation(
                f"hydra production wrote to disallowed path: {path}"
            )

    def guarded_open(file, mode="r", *args, **kwargs):
        if isinstance(mode, str) and any(m in mode for m in ("w", "a", "x", "+")):
            _check(file)
        return orig_open(file, mode, *args, **kwargs)

    def guarded_write_text(self, *args, **kwargs):
        _check(self)
        return orig_write_text(self, *args, **kwargs)

    def guarded_write_bytes(self, *args, **kwargs):
        _check(self)
        return orig_write_bytes(self, *args, **kwargs)

    def guarded_fsync(fd):
        # fsync takes an fd, not a path; in Phase 1 we pass through. The
        # guarded_open intercept already caught the write that produced this
        # fd if the path was disallowed.
        return orig_fsync(fd)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(Path, "write_text", guarded_write_text)
    monkeypatch.setattr(Path, "write_bytes", guarded_write_bytes)
    monkeypatch.setattr(os, "fsync", guarded_fsync)
    yield
