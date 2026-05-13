"""Tests for the central model registry in hydra.models."""

from __future__ import annotations

from pathlib import Path

import pytest

from hydra import models
from hydra.models import TIERS, ModelSpec, default_tiers, load_model_tiers


def test_default_tiers_keys_equal_tiers() -> None:
    tiers = default_tiers()
    assert set(tiers.keys()) == set(TIERS)


def test_every_tier_has_non_empty_list_of_specs() -> None:
    tiers = default_tiers()
    for tier_name, specs in tiers.items():
        assert isinstance(specs, list), f"{tier_name} value is not a list"
        assert len(specs) > 0, f"{tier_name} has no specs"
        for spec in specs:
            assert isinstance(spec, ModelSpec)


def test_default_tiers_returns_independent_copy() -> None:
    a = default_tiers()
    b = default_tiers()
    a["watcher"].append(ModelSpec(cli="vibe", model="x"))
    assert len(b["watcher"]) != len(a["watcher"]), "default_tiers must hand out copies"


def test_watcher_primary_is_claude_haiku() -> None:
    primary = default_tiers()["watcher"][0]
    assert primary.cli == "claude"
    assert primary.model == "claude-haiku-4-5"
    assert primary.effort_flag is None
    assert primary.hard_timeout_s == 10.0


def test_watcher_fallback_is_codex_gpt54_mini() -> None:
    fallback = default_tiers()["watcher"][1]
    assert fallback.cli == "codex"
    assert fallback.model == "gpt-5.4-mini"
    assert fallback.hard_timeout_s == 10.0


def test_fast_tier_has_three_entries_claude_codex_gemini() -> None:
    fast = default_tiers()["fast"]
    assert len(fast) == 3
    assert [s.cli for s in fast] == ["claude", "codex", "gemini"]
    for spec in fast:
        assert spec.hard_timeout_s == 60.0
    assert fast[2].model == "gemini-2.5-flash"


def test_heavy_tier_has_three_entries_with_correct_models() -> None:
    heavy = default_tiers()["heavy"]
    assert len(heavy) == 3
    assert [s.cli for s in heavy] == ["claude", "codex", "gemini"]
    assert heavy[0].model == "claude-opus-4-7"
    assert heavy[1].model == "gpt-5.5"
    assert heavy[2].model == "gemini-2.5-pro"
    for spec in heavy:
        assert spec.hard_timeout_s == 300.0


def test_heavy_codex_has_effort_high_flag() -> None:
    heavy = default_tiers()["heavy"]
    codex = next(s for s in heavy if s.cli == "codex")
    assert codex.effort_flag == "--effort high"


def test_heavy_claude_and_gemini_have_no_effort_flag() -> None:
    heavy = default_tiers()["heavy"]
    claude = next(s for s in heavy if s.cli == "claude")
    gemini = next(s for s in heavy if s.cli == "gemini")
    assert claude.effort_flag is None
    assert gemini.effort_flag is None


def test_model_spec_to_id_format() -> None:
    spec = ModelSpec(cli="claude", model="claude-haiku-4-5")
    assert spec.to_id() == "claude:claude-haiku-4-5"


def test_model_spec_to_id_with_complex_model_name() -> None:
    spec = ModelSpec(cli="local-mlx", model="mlx-community/gemma-3-4b-it-4bit")
    assert spec.to_id() == "local-mlx:mlx-community/gemma-3-4b-it-4bit"


def test_load_model_tiers_with_none_returns_defaults() -> None:
    assert load_model_tiers(None) == default_tiers()


def test_load_model_tiers_with_nonexistent_path_returns_defaults(
    tmp_path: Path,
) -> None:
    bogus = tmp_path / "does-not-exist.toml"
    assert load_model_tiers(bogus) == default_tiers()


def test_load_model_tiers_with_empty_toml_returns_defaults(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("[defaults]\nweb_port = 4125\n", encoding="utf-8")
    assert load_model_tiers(cfg) == default_tiers()


def test_load_model_tiers_watcher_shorthand_primary_and_fallback(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[defaults.models.watcher]\n"
        'primary = "vibe:mistral-large-3"\n'
        'fallback = "local-mlx:my-model"\n',
        encoding="utf-8",
    )
    tiers = load_model_tiers(cfg)
    assert tiers["watcher"] == [
        ModelSpec(cli="vibe", model="mistral-large-3", hard_timeout_s=10.0),
        ModelSpec(cli="local-mlx", model="my-model", hard_timeout_s=10.0),
    ]


def test_load_model_tiers_watcher_shorthand_primary_only_uses_default_fallback(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[defaults.models.watcher]\nprimary = "vibe:mistral-large-3"\n',
        encoding="utf-8",
    )
    tiers = load_model_tiers(cfg)
    assert tiers["watcher"][0] == ModelSpec(
        cli="vibe", model="mistral-large-3", hard_timeout_s=10.0
    )
    default_fallback = default_tiers()["watcher"][1]
    assert tiers["watcher"][1] == default_fallback


def test_load_model_tiers_fast_array_form_single_entry(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[[defaults.models.fast]]\n"
        'cli = "vibe"\n'
        'model = "mistral-medium-3"\n'
        "hard_timeout_s = 45.0\n",
        encoding="utf-8",
    )
    tiers = load_model_tiers(cfg)
    assert tiers["fast"] == [
        ModelSpec(cli="vibe", model="mistral-medium-3", hard_timeout_s=45.0)
    ]


def test_load_model_tiers_heavy_array_with_effort_flag(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[[defaults.models.heavy]]\n"
        'cli = "vibe"\n'
        'model = "mistral-xl"\n'
        'effort_flag = "--reasoning extra"\n',
        encoding="utf-8",
    )
    tiers = load_model_tiers(cfg)
    assert len(tiers["heavy"]) == 1
    spec = tiers["heavy"][0]
    assert spec.cli == "vibe"
    assert spec.model == "mistral-xl"
    assert spec.effort_flag == "--reasoning extra"
    assert spec.hard_timeout_s == 300.0


def test_load_model_tiers_mixed_override_only_heavy(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[[defaults.models.heavy]]\ncli = "vibe"\nmodel = "mistral-xl"\n',
        encoding="utf-8",
    )
    tiers = load_model_tiers(cfg)
    defaults = default_tiers()
    assert tiers["watcher"] == defaults["watcher"]
    assert tiers["fast"] == defaults["fast"]
    assert tiers["heavy"] == [
        ModelSpec(cli="vibe", model="mistral-xl", hard_timeout_s=300.0)
    ]


def test_load_model_tiers_missing_cli_raises_with_tier_and_index(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[[defaults.models.fast]]\nmodel = "mistral-medium-3"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        load_model_tiers(cfg)
    msg = str(excinfo.value)
    assert "fast" in msg
    assert "0" in msg
    assert "cli" in msg


def test_load_model_tiers_missing_model_raises_with_tier_and_index(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[[defaults.models.fast]]\n"
        'cli = "claude"\n'
        'model = "claude-haiku-4-5"\n'
        "\n"
        "[[defaults.models.fast]]\n"
        'cli = "codex"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        load_model_tiers(cfg)
    msg = str(excinfo.value)
    assert "fast" in msg
    assert "1" in msg
    assert "model" in msg


def test_load_model_tiers_watcher_primary_malformed_raises(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[defaults.models.watcher]\nprimary = "no-colon-here"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        load_model_tiers(cfg)
    msg = str(excinfo.value)
    assert "watcher" in msg
    assert "primary" in msg


def test_models_module_exposes_tiers_constant() -> None:
    assert hasattr(models, "TIERS")
    assert models.TIERS == ("watcher", "fast", "heavy")
