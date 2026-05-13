"""Tests for hydra.cli_check: CLI tool availability probe."""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

import pytest

from hydra.cli_check import (
    HYDRA_REQUIRED_CLIS,
    INSTALL_HINTS,
    AllToolsMissingError,
    CheckResult,
    available_set,
    check_all,
    check_cli,
    preflight_check,
)


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def fake_run_factory(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    timeout: bool = False,
    callback: Any = None,
):
    def _run(cmd, **kwargs):
        if callback is not None:
            callback(cmd, kwargs)
        if timeout:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0.0))
        return _FakeCompleted(returncode=returncode, stdout=stdout, stderr=stderr)

    return _run


def test_tool_on_path_version_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/bin/claude")
    run = fake_run_factory(returncode=0, stdout="Claude Code v1.2.3\n")

    result = check_cli("claude", run=run)

    assert isinstance(result, CheckResult)
    assert result.name == "claude"
    assert result.present is True
    assert result.path == "/fake/bin/claude"
    assert result.version == "Claude Code v1.2.3"
    assert result.error is None
    assert result.install_hint is None


def test_tool_not_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    result = check_cli("claude", run=fake_run_factory())

    assert result.name == "claude"
    assert result.present is False
    assert result.path is None
    assert result.version is None
    assert result.error == "claude not on PATH"
    assert result.install_hint == INSTALL_HINTS["claude"]


def test_tool_on_path_but_version_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/bin/codex")
    run = fake_run_factory(timeout=True)

    result = check_cli("codex", run=run)

    assert result.present is True
    assert result.path == "/fake/bin/codex"
    assert result.version is None
    assert result.error is not None
    assert "timed out" in result.error.lower()
    assert result.install_hint is None


def test_tool_on_path_but_version_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/bin/gemini")
    run = fake_run_factory(returncode=1, stderr="boom: something failed")

    result = check_cli("gemini", run=run)

    assert result.present is True
    assert result.path == "/fake/bin/gemini"
    assert result.version is None
    assert result.error is not None
    assert "1" in result.error
    assert "boom: something failed" in result.error
    assert result.install_hint is None


def test_multiline_version_output_takes_first_nonempty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/bin/codex")
    run = fake_run_factory(returncode=0, stdout="\n\nCodex v2\nbuilt: 2026-05-13\n")

    result = check_cli("codex", run=run)

    assert result.version == "Codex v2"


def test_version_falls_back_to_stderr_when_stdout_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/bin/vibe")
    run = fake_run_factory(returncode=0, stdout="", stderr="vibe 0.9.0\n")

    result = check_cli("vibe", run=run)

    assert result.version == "vibe 0.9.0"
    assert result.error is None


def test_check_all_checks_every_configured_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/fake/bin/{name}")
    run = fake_run_factory(returncode=0, stdout="some v1\n")

    results = check_all(run=run)

    assert set(results.keys()) == set(HYDRA_REQUIRED_CLIS)
    for name, res in results.items():
        assert res.name == name
        assert res.present is True
        assert res.version == "some v1"


def test_check_all_uses_custom_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/fake/bin/{name}")
    run = fake_run_factory(returncode=0, stdout="v1\n")

    results = check_all(names=("claude", "codex"), run=run)

    assert set(results.keys()) == {"claude", "codex"}


def test_available_set_filters_to_present_and_no_error() -> None:
    results = {
        "claude": CheckResult(
            name="claude",
            present=True,
            path="/x/claude",
            version="v1",
            error=None,
            install_hint=None,
        ),
        "codex": CheckResult(
            name="codex",
            present=False,
            path=None,
            version=None,
            error="codex not on PATH",
            install_hint=INSTALL_HINTS["codex"],
        ),
        "gemini": CheckResult(
            name="gemini",
            present=True,
            path="/x/gemini",
            version=None,
            error="exit code 1: bad",
            install_hint=None,
        ),
        "vibe": CheckResult(
            name="vibe",
            present=True,
            path="/x/vibe",
            version="v0.9",
            error=None,
            install_hint=None,
        ),
    }

    assert available_set(results) == {"claude", "vibe"}


def test_preflight_check_all_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(AllToolsMissingError):
        preflight_check(run=fake_run_factory())


def test_preflight_check_partial_missing_returns_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_which(name: str) -> str | None:
        if name == "vibe":
            return None
        return f"/fake/bin/{name}"

    monkeypatch.setattr(shutil, "which", fake_which)
    run = fake_run_factory(returncode=0, stdout="v1\n")

    results = preflight_check(run=run)

    assert set(results.keys()) == set(HYDRA_REQUIRED_CLIS)
    assert results["vibe"].present is False
    assert results["claude"].present is True


def test_preflight_check_all_errored_also_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/fake/bin/{name}")
    run = fake_run_factory(returncode=2, stderr="nope")

    with pytest.raises(AllToolsMissingError):
        preflight_check(run=run)


def test_unknown_tool_name_no_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    result = check_cli("foobar", run=fake_run_factory())

    assert result.present is False
    assert result.install_hint is None
    assert result.error == "foobar not on PATH"


def test_run_receives_version_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/bin/claude")
    captured: dict[str, Any] = {}

    def _capture(cmd, kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs

    run = fake_run_factory(returncode=0, stdout="v1\n", callback=_capture)

    check_cli("claude", timeout_s=2.5, run=run)

    assert captured["cmd"] == ["/fake/bin/claude", "--version"]
    assert captured["kwargs"]["timeout"] == 2.5
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True
    assert captured["kwargs"]["check"] is False


@pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="claude CLI not on real PATH; skipping live smoke test",
)
def test_real_claude_on_path_smoke() -> None:
    result = check_cli("claude")
    assert result.present is True
    assert result.path is not None
    assert result.version is not None and result.version.strip() != ""
