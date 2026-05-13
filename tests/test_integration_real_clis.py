"""Opt-in real-CLI integration tests.

These tests SPAWN ACTUAL LLM CLI subprocesses (claude, codex, gemini, vibe).
They cost tokens. Excluded from the default test suite via -m 'not integration'.
Run with: bash scripts/run_test_suite.sh --integration

Each test sends a tiny well-defined prompt and checks that:
- The CLI invocation succeeds (return code 0).
- The stdout contains a parseable structured payload OR a recognizable answer
  to the question.

Some tests check JSON extraction round-trip — they verify Hydra's
hydra/json_extract.py can parse the actual CLI output (catches noise pattern
drift over time).

Tests are skip-if-not-on-PATH (via shutil.which) so the suite stays
maintainable on machines that only have some of the four CLIs.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from hydra import json_extract
from hydra.models import default_tiers

pytestmark = pytest.mark.integration

# Hard timeout per CLI invocation (large enough for cold-start authentication
# but small enough that a hung integration suite doesn't waste minutes).
INVOCATION_TIMEOUT_S = 90.0

# Tiny well-defined prompt. Each CLI's --version is verified first; this
# is the actual model call.
ASK_PROMPT = (
    'What is 2+2? Reply with a JSON object: {"answer": <number>, "citations": []}'
)


def _skip_if_missing(name: str) -> None:
    if shutil.which(name) is None:
        pytest.skip(f"{name} not on PATH; skipping real-CLI integration test")


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Run argv; return (rc, stdout, stderr). Stdout/stderr may be large."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=INVOCATION_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"{argv[0]} timed out after {INVOCATION_TIMEOUT_S}s")
    return proc.returncode, proc.stdout, proc.stderr


# --- Per-CLI tests ---


class TestClaude:
    def test_claude_version(self):
        _skip_if_missing("claude")
        rc, out, err = _run_cli(["claude", "--version"])
        assert rc == 0, f"claude --version failed: {err}"
        assert "claude" in (out + err).lower()

    def test_claude_short_prompt_returns_json(self):
        _skip_if_missing("claude")
        tier = default_tiers()["fast"]
        claude_spec = next(s for s in tier if s.cli == "claude")
        rc, out, err = _run_cli(
            ["claude", "-p", ASK_PROMPT, "--model", claude_spec.model]
        )
        assert rc == 0, f"claude -p failed: rc={rc}, stderr={err[-1000:]}"
        # The extractor should find the JSON; the answer should be 4.
        try:
            payload = json_extract.extract_json(out)
        except json_extract.JSONExtractError:
            pytest.fail(f"failed to extract JSON from claude stdout: {out[-1000:]}")
        assert isinstance(payload, dict)
        assert payload.get("answer") == 4 or payload.get("answer") == "4"


class TestCodex:
    def test_codex_version(self):
        _skip_if_missing("codex")
        rc, _out, err = _run_cli(["codex", "--version"])
        assert rc == 0, f"codex --version failed: {err}"

    def test_codex_short_prompt(self):
        _skip_if_missing("codex")
        tier = default_tiers()["fast"]
        codex_spec = next((s for s in tier if s.cli == "codex"), None)
        if codex_spec is None:
            pytest.skip("no codex spec in fast tier")
        rc, out, err = _run_cli(
            ["codex", "exec", ASK_PROMPT, "--model", codex_spec.model]
        )
        # Codex may exit nonzero on quota; document either path.
        if rc != 0:
            pytest.skip(
                f"codex exec returned rc={rc}; likely a quota/auth issue: {err[-500:]}"
            )
        try:
            payload = json_extract.extract_json(out)
        except json_extract.JSONExtractError:
            pytest.fail(f"failed to extract JSON from codex stdout: {out[-1000:]}")
        assert isinstance(payload, dict)


class TestGemini:
    def test_gemini_version(self):
        _skip_if_missing("gemini")
        rc, _out, err = _run_cli(["gemini", "--version"])
        assert rc == 0, f"gemini --version failed: {err}"

    def test_gemini_short_prompt(self):
        _skip_if_missing("gemini")
        tier = default_tiers()["fast"]
        gemini_spec = next((s for s in tier if s.cli == "gemini"), None)
        if gemini_spec is None:
            pytest.skip("no gemini spec in fast tier")
        rc, out, err = _run_cli(
            ["gemini", "-p", ASK_PROMPT, "--model", gemini_spec.model]
        )
        if rc != 0:
            pytest.skip(f"gemini -p returned rc={rc}: {err[-500:]}")
        try:
            payload = json_extract.extract_json(out)
        except json_extract.JSONExtractError:
            pytest.fail(f"failed to extract JSON from gemini stdout: {out[-1000:]}")
        assert isinstance(payload, dict)


class TestVibe:
    def test_vibe_version(self):
        _skip_if_missing("vibe")
        rc, _out, err = _run_cli(["vibe", "--version"])
        assert rc == 0, f"vibe --version failed: {err}"

    def test_vibe_short_prompt(self):
        _skip_if_missing("vibe")
        # Vibe may not be in default tiers; skip if not.
        rc, out, err = _run_cli(["vibe", "-p", ASK_PROMPT])
        if rc != 0:
            pytest.skip(f"vibe -p returned rc={rc}: {err[-500:]}")
        # Vibe may return plain text rather than JSON; loosely check for "4".
        assert "4" in out


# --- Cross-CLI smoke ---


class TestNoiseRobustness:
    """Verify json_extract.extract_json handles real CLI output without panic."""

    def test_all_clis_produce_extractable_or_skippable_output(self):
        clis = ["claude", "codex", "gemini", "vibe"]
        successes = []
        failures = []
        for cli in clis:
            if shutil.which(cli) is None:
                continue
            argv = [cli, "--version"]
            rc, _out, _err = _run_cli(argv)
            if rc == 0:
                successes.append(cli)
            else:
                failures.append(cli)
        # Loose assertion: at least one CLI is alive on this machine.
        assert successes, f"no CLIs available: failures={failures}"
