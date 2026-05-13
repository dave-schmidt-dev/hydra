from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

Tier = Literal["watcher", "fast", "heavy"]
TIERS: tuple[Tier, ...] = get_args(Tier)


@dataclass(frozen=True)
class ModelSpec:
    cli: str
    model: str
    effort_flag: str | None = None
    hard_timeout_s: float = 60.0

    def to_id(self) -> str:
        return f"{self.cli}:{self.model}"


_TIER_DEFAULT_TIMEOUTS: dict[Tier, float] = {
    "watcher": 10.0,
    "fast": 60.0,
    "heavy": 300.0,
}

_DEFAULT_TIERS: dict[Tier, list[ModelSpec]] = {
    "watcher": [
        ModelSpec(cli="claude", model="claude-haiku-4-5", hard_timeout_s=10.0),
        ModelSpec(cli="codex", model="gpt-5.4-mini", hard_timeout_s=10.0),
    ],
    "fast": [
        ModelSpec(cli="claude", model="claude-haiku-4-5", hard_timeout_s=60.0),
        ModelSpec(cli="codex", model="gpt-5.4-mini", hard_timeout_s=60.0),
        ModelSpec(cli="gemini", model="gemini-2.5-flash", hard_timeout_s=60.0),
    ],
    "heavy": [
        ModelSpec(cli="claude", model="claude-opus-4-7", hard_timeout_s=300.0),
        ModelSpec(
            cli="codex",
            model="gpt-5.5",
            effort_flag="--effort high",
            hard_timeout_s=300.0,
        ),
        ModelSpec(cli="gemini", model="gemini-2.5-pro", hard_timeout_s=300.0),
    ],
}


def default_tiers() -> dict[Tier, list[ModelSpec]]:
    return {tier: list(specs) for tier, specs in _DEFAULT_TIERS.items()}


def _parse_cli_model(value: Any, key_path: str, tier: Tier) -> ModelSpec:
    if not isinstance(value, str) or ":" not in value:
        raise ValueError(
            f"config.toml [defaults.models.{tier}].{key_path}: "
            f"expected 'cli:model' string, got {value!r}"
        )
    cli, _, model = value.partition(":")
    if not cli or not model:
        raise ValueError(
            f"config.toml [defaults.models.{tier}].{key_path}: "
            f"both cli and model must be non-empty in {value!r}"
        )
    return ModelSpec(cli=cli, model=model, hard_timeout_s=_TIER_DEFAULT_TIMEOUTS[tier])


def _parse_spec_table(entry: Any, tier: Tier, index: int) -> ModelSpec:
    if not isinstance(entry, dict):
        raise ValueError(
            f"config.toml [defaults.models.{tier}][{index}]: "
            f"expected table, got {type(entry).__name__}"
        )
    if "cli" not in entry:
        raise ValueError(
            f"config.toml [defaults.models.{tier}][{index}]: "
            f"missing required field 'cli'"
        )
    if "model" not in entry:
        raise ValueError(
            f"config.toml [defaults.models.{tier}][{index}]: "
            f"missing required field 'model'"
        )
    return ModelSpec(
        cli=str(entry["cli"]),
        model=str(entry["model"]),
        effort_flag=entry.get("effort_flag"),
        hard_timeout_s=float(entry.get("hard_timeout_s", _TIER_DEFAULT_TIMEOUTS[tier])),
    )


def _parse_watcher_override(
    table: dict[str, Any], defaults: list[ModelSpec]
) -> list[ModelSpec]:
    if "primary" not in table:
        raise ValueError(
            "config.toml [defaults.models.watcher]: "
            "missing required field 'primary' (use 'cli:model' string)"
        )
    primary = _parse_cli_model(table["primary"], "primary", "watcher")
    if "fallback" in table:
        fallback = _parse_cli_model(table["fallback"], "fallback", "watcher")
    else:
        fallback = defaults[1] if len(defaults) > 1 else defaults[0]
    return [primary, fallback]


def load_model_tiers(config_path: Path | None = None) -> dict[Tier, list[ModelSpec]]:
    tiers = default_tiers()
    if config_path is None or not Path(config_path).exists():
        return tiers

    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    models_section = data.get("defaults", {}).get("models")
    if not models_section:
        return tiers

    for tier in TIERS:
        if tier not in models_section:
            continue
        override = models_section[tier]
        if tier == "watcher" and isinstance(override, dict):
            tiers[tier] = _parse_watcher_override(override, tiers[tier])
        elif isinstance(override, list):
            tiers[tier] = [
                _parse_spec_table(entry, tier, i) for i, entry in enumerate(override)
            ]
        else:
            raise ValueError(
                f"config.toml [defaults.models.{tier}]: "
                f"expected array of tables or primary/fallback table, "
                f"got {type(override).__name__}"
            )

    return tiers
