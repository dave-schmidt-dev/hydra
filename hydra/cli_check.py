"""CLI tool availability check.

Probes whether each configured CLI (claude/codex/gemini/vibe) is on PATH and
responds to ``--version``. Results feed quota routing so missing-tool tier
members can be excluded from the routing pool.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass

logger = logging.getLogger("hydra.cli_check")

HYDRA_REQUIRED_CLIS: tuple[str, ...] = ("claude", "codex", "gemini", "vibe")

INSTALL_HINTS: dict[str, str] = {
    "claude": (
        "Install Claude Code: https://docs.anthropic.com/en/docs/claude-code "
        "(npm install -g @anthropic-ai/claude-code)."
    ),
    "codex": (
        "Install OpenAI Codex CLI: https://github.com/openai/codex-cli "
        "(npm install -g @openai/codex)."
    ),
    "gemini": (
        "Install Google Gemini CLI: brew install gemini-cli "
        "(or follow the project README)."
    ),
    "vibe": (
        "Install Mistral Vibe CLI: follow the Mistral docs to install the vibe tool."
    ),
}


@dataclass(frozen=True)
class CheckResult:
    name: str
    present: bool
    path: str | None
    version: str | None
    error: str | None
    install_hint: str | None


def _first_nonempty_line(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def check_cli(
    name: str,
    *,
    timeout_s: float = 5.0,
    run: object | None = None,
) -> CheckResult:
    runner = run if run is not None else subprocess.run
    path = shutil.which(name)
    if path is None:
        return CheckResult(
            name=name,
            present=False,
            path=None,
            version=None,
            error=f"{name} not on PATH",
            install_hint=INSTALL_HINTS.get(name),
        )

    try:
        completed = runner(  # type: ignore[operator]
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("cli_check: %s --version timed out after %.1fs", name, timeout_s)
        return CheckResult(
            name=name,
            present=True,
            path=path,
            version=None,
            error=f"{name} --version timed out after {timeout_s}s",
            install_hint=None,
        )

    returncode = completed.returncode
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""

    if returncode == 0:
        version = _first_nonempty_line(stdout) or _first_nonempty_line(stderr)
        return CheckResult(
            name=name,
            present=True,
            path=path,
            version=version,
            error=None,
            install_hint=None,
        )

    err_snippet = (stderr or stdout).strip()
    return CheckResult(
        name=name,
        present=True,
        path=path,
        version=None,
        error=f"{name} --version exited with code {returncode}: {err_snippet}",
        install_hint=None,
    )


def check_all(
    names: Iterable[str] = HYDRA_REQUIRED_CLIS,
    *,
    timeout_s: float = 5.0,
    run: object | None = None,
) -> dict[str, CheckResult]:
    return {name: check_cli(name, timeout_s=timeout_s, run=run) for name in names}


def available_set(results: dict[str, CheckResult]) -> set[str]:
    return {name for name, r in results.items() if r.present and r.error is None}


class AllToolsMissingError(RuntimeError):
    """Raised when preflight_check finds zero usable tools."""


def preflight_check(
    names: Iterable[str] = HYDRA_REQUIRED_CLIS,
    *,
    timeout_s: float = 5.0,
    run: object | None = None,
) -> dict[str, CheckResult]:
    results = check_all(names, timeout_s=timeout_s, run=run)
    if not available_set(results):
        raise AllToolsMissingError(
            "no configured CLI tools are available; install at least one of "
            f"{tuple(results.keys())}"
        )
    return results
