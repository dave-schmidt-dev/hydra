"""Smoke tests for the initial scaffold."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_metadata_sane() -> None:
    text = (ROOT / "pyproject.toml").read_text()
    assert 'name = "hydra"' in text
    assert "requires-python" in text
    assert "hatchling" in text


def test_readme_present() -> None:
    assert (ROOT / "README.md").exists()


def test_history_present() -> None:
    assert (ROOT / "HISTORY.md").exists()


def test_tasks_present() -> None:
    assert (ROOT / "TASKS.md").exists()


def test_license_present() -> None:
    license_text = (ROOT / "LICENSE").read_text()
    assert "MIT License" in license_text


def test_cli_help_runs() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "hydra", "--help"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert proc.returncode == 0
    assert "hydra start" in proc.stdout
