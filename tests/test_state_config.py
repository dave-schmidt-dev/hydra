"""Tests for the config-table helpers in hydra.state."""

from __future__ import annotations

from pathlib import Path

import pytest

from hydra import state


@pytest.fixture(autouse=True)
def _reset_breaker() -> None:
    state._reset_breaker_for_tests()
    yield
    state._reset_breaker_for_tests()


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    state.init_session_db(tmp_path)
    return tmp_path


def test_set_then_get_roundtrip_string(session_dir: Path) -> None:
    state.set_config(session_dir, "foo", "bar")
    assert state.get_config(session_dir, "foo") == "bar"


def test_get_missing_returns_default(session_dir: Path) -> None:
    assert state.get_config(session_dir, "missing") is None
    assert state.get_config(session_dir, "missing", default=42) == 42


def test_set_config_idempotent_overwrites(session_dir: Path) -> None:
    state.set_config(session_dir, "k", "v1")
    state.set_config(session_dir, "k", "v2")
    assert state.get_config(session_dir, "k") == "v2"


def test_set_config_json_encodes_dict(session_dir: Path) -> None:
    payload = {"a": 1, "b": [1, 2, 3], "c": {"nested": True}}
    state.set_config(session_dir, "dict_key", payload)
    assert state.get_config(session_dir, "dict_key") == payload


def test_set_config_json_encodes_list_and_int(session_dir: Path) -> None:
    state.set_config(session_dir, "lst", [1, 2, 3])
    state.set_config(session_dir, "n", 12345)
    assert state.get_config(session_dir, "lst") == [1, 2, 3]
    assert state.get_config(session_dir, "n") == 12345


def test_set_config_float_roundtrip(session_dir: Path) -> None:
    state.set_config(session_dir, "elapsed", 12.345)
    assert state.get_config(session_dir, "elapsed") == 12.345
