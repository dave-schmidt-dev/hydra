"""Tests for the quota router (Phase 3.1).

The router wraps ai_monitor as a subprocess, caches snapshots, blacklists
providers on 429s, and routes per tier by picking the candidate with the
highest *shortest-window* remaining percentage. When ai_monitor errors
or its schema doesn't validate, the router round-robins among candidates.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field

import pytest

from hydra.ai_monitor_schema import AiMonitorSnapshot
from hydra.quota import (
    BLACKLIST_SECONDS,
    CACHE_TTL_SECONDS,
    NoCandidateModelError,
    QuotaRouter,
)


@dataclass
class VirtualClock:
    now: float = 0.0

    def read(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class EventCapture:
    events: list[tuple[str, dict]] = field(default_factory=list)

    def __call__(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))

    def types(self) -> list[str]:
        return [event_type for event_type, _ in self.events]


def make_router(
    *,
    fetch=None,
    cli_available=None,
    clock=None,
    tiers=None,
):
    capture = EventCapture()
    kwargs: dict = {"fetch": fetch, "on_event": capture}
    if cli_available is not None:
        kwargs["cli_available"] = cli_available
    if clock is not None:
        kwargs["clock"] = clock
    if tiers is not None:
        kwargs["tiers"] = tiers
    return QuotaRouter(**kwargs), capture


def make_snapshot_dict(providers_pct: dict[str, list[float]]) -> dict:
    """Build a snapshot dict mimicking ai_monitor's --json output.

    providers_pct maps provider name (Capitalized) to a list of
    remaining-percent values, one per window.
    """
    providers = []
    for name, pcts in providers_pct.items():
        # Map provider name to one of its known fields. Use Codex shape
        # (five_hour_percent_left + weekly_percent_left) for Codex / Claude,
        # Gemini shape for Gemini, and Vibe (usage_percent = % used).
        if name == "Codex":
            data = {
                "five_hour_percent_left": int(pcts[0]),
                "weekly_percent_left": int(pcts[1]) if len(pcts) > 1 else None,
                "credits": None,
                "five_hour_reset": None,
                "weekly_reset": None,
            }
        elif name == "Claude":
            data = {
                "session_percent_left": int(pcts[0]),
                "weekly_percent_left": int(pcts[1]) if len(pcts) > 1 else None,
                "opus_percent_left": None,
                "primary_reset": None,
                "secondary_reset": None,
                "opus_reset": None,
                "account_email": None,
                "account_organization": None,
                "login_method": None,
            }
        elif name == "Gemini":
            data = {
                "flash_percent_left": int(pcts[0]),
                "pro_percent_left": int(pcts[1]) if len(pcts) > 1 else None,
                "flash_reset": None,
                "pro_reset": None,
                "account_email": None,
                "account_tier": None,
            }
        elif name == "Vibe":
            # ai_monitor reports % USED; remaining = 100 - used.
            data = {
                "usage_percent": 100.0 - float(pcts[0]),
                "reset_at": None,
                "payg_enabled": None,
                "start_date": None,
                "end_date": None,
            }
        else:
            raise ValueError(f"unsupported provider name in fixture: {name}")
        providers.append(
            {
                "name": name,
                "ok": True,
                "source": "api",
                "data": data,
                "display": {},
                "error": None,
            }
        )
    return {
        "updated_at": "2026-05-13T08:00:00",
        "providers": providers,
    }


def test_pick_model_from_snapshot_single_window_chooses_highest_remaining() -> None:
    snap = make_snapshot_dict(
        {
            "Claude": [70],
            "Codex": [40],
            "Gemini": [85],
        }
    )
    router, _ = make_router(fetch=lambda: snap)

    chosen = router.pick_model("fast")

    assert chosen.cli == "gemini"


def test_pick_model_tiebreak_uses_tier_order() -> None:
    snap = make_snapshot_dict({"Claude": [50], "Codex": [50], "Gemini": [50]})
    router, _ = make_router(fetch=lambda: snap)

    chosen = router.pick_model("fast")

    assert chosen.cli == "claude"


def test_pick_model_all_low_emits_quota_low_event() -> None:
    snap = make_snapshot_dict({"Claude": [2], "Codex": [3], "Gemini": [4]})
    router, events = make_router(fetch=lambda: snap)

    chosen = router.pick_model("fast")

    assert chosen.cli == "gemini"
    assert "quota_low" in events.types()


def test_pick_model_uses_shortest_window_remaining() -> None:
    # Claude shortest = 30, Gemini shortest = 60 → Gemini wins despite
    # Claude having a higher 5h window.
    snap = make_snapshot_dict({"Claude": [80, 30], "Codex": [25, 25], "Gemini": [60]})
    router, _ = make_router(fetch=lambda: snap)

    chosen = router.pick_model("fast")

    assert chosen.cli == "gemini"


def test_blacklisted_provider_is_excluded() -> None:
    snap = make_snapshot_dict({"Claude": [90], "Codex": [40], "Gemini": [50]})
    router, _ = make_router(fetch=lambda: snap)

    router.mark_blacklisted("claude")
    chosen = router.pick_model("fast")

    assert chosen.cli != "claude"
    assert chosen.cli == "gemini"


def test_blacklist_expires_after_window() -> None:
    snap = make_snapshot_dict({"Claude": [90], "Codex": [40], "Gemini": [50]})
    clock = VirtualClock()
    router, _ = make_router(fetch=lambda: snap, clock=clock.read)

    router.mark_blacklisted("claude")
    clock.advance(BLACKLIST_SECONDS + 1)

    chosen = router.pick_model("fast")

    assert chosen.cli == "claude"


def test_cli_availability_filters_specs() -> None:
    snap = make_snapshot_dict({"Claude": [50], "Codex": [50], "Gemini": [99]})
    router, _ = make_router(fetch=lambda: snap, cli_available={"claude"})

    chosen = router.pick_model("fast")

    assert chosen.cli == "claude"


def test_no_candidates_raises() -> None:
    snap = make_snapshot_dict({"Claude": [50], "Codex": [50], "Gemini": [50]})
    router, _ = make_router(fetch=lambda: snap)

    router.mark_blacklisted("claude")
    router.mark_blacklisted("codex")
    router.mark_blacklisted("gemini")

    with pytest.raises(NoCandidateModelError):
        router.pick_model("fast")


def test_fetch_failure_round_robins_among_candidates() -> None:
    router, events = make_router(fetch=lambda: None)

    first = router.pick_model("fast")
    second = router.pick_model("fast")
    third = router.pick_model("fast")

    assert [first.cli, second.cli, third.cli] == ["claude", "codex", "gemini"]
    assert "ai_monitor_unavailable" in events.types()


def test_schema_mismatch_round_robins_and_emits_event() -> None:
    router, events = make_router(fetch=lambda: {"not": "the schema"})

    first = router.pick_model("fast")
    second = router.pick_model("fast")

    assert first.cli == "claude"
    assert second.cli == "codex"
    assert "schema_mismatch" in events.types()


def test_cache_ttl_avoids_refetch_within_window() -> None:
    calls = {"n": 0}

    def fetcher():
        calls["n"] += 1
        return make_snapshot_dict({"Claude": [50], "Codex": [40], "Gemini": [30]})

    clock = VirtualClock()
    router, _ = make_router(fetch=fetcher, clock=clock.read)

    router.pick_model("fast")
    router.pick_model("fast")

    assert calls["n"] == 1


def test_cache_refresh_after_ttl_expiry() -> None:
    calls = {"n": 0}

    def fetcher():
        calls["n"] += 1
        return make_snapshot_dict({"Claude": [50], "Codex": [40], "Gemini": [30]})

    clock = VirtualClock()
    router, _ = make_router(fetch=fetcher, clock=clock.read)

    router.pick_model("fast")
    clock.advance(CACHE_TTL_SECONDS + 1)
    router.pick_model("fast")

    assert calls["n"] == 2


def test_schema_mismatch_event_emitted_once_then_silenced() -> None:
    router, events = make_router(fetch=lambda: {"not": "the schema"})

    router.pick_model("fast")
    router.pick_model("fast")
    router.pick_model("fast")

    schema_events = [ev for ev in events.types() if ev == "schema_mismatch"]
    assert len(schema_events) == 1


def test_unknown_provider_skipped_during_pct_lookup() -> None:
    # If snapshot lists only one of our candidates, the missing ones get
    # a default of 0% remaining (worst-case) so the present provider wins.
    snap = make_snapshot_dict({"Gemini": [10]})
    router, _ = make_router(fetch=lambda: snap)

    chosen = router.pick_model("fast")

    assert chosen.cli == "gemini"


def test_provider_with_error_treated_as_zero_remaining() -> None:
    # Provider in snapshot but ok=False (errored): treat as 0% so it
    # never wins. Tied losers fall back to tier order.
    raw = {
        "updated_at": "2026-05-13T08:00:00",
        "providers": [
            {
                "name": "Claude",
                "ok": False,
                "source": "api",
                "data": None,
                "display": {},
                "error": "rate limited",
            },
            {
                "name": "Codex",
                "ok": True,
                "source": "api",
                "data": {
                    "five_hour_percent_left": 10,
                    "weekly_percent_left": 10,
                    "credits": None,
                    "five_hour_reset": None,
                    "weekly_reset": None,
                },
                "display": {},
                "error": None,
            },
            {
                "name": "Gemini",
                "ok": True,
                "source": "api",
                "data": {
                    "flash_percent_left": 5,
                    "pro_percent_left": 5,
                    "flash_reset": None,
                    "pro_reset": None,
                    "account_email": None,
                    "account_tier": None,
                },
                "display": {},
                "error": None,
            },
        ],
    }
    router, _ = make_router(fetch=lambda: raw)

    chosen = router.pick_model("fast")

    assert chosen.cli == "codex"


def test_ai_monitor_schema_parses_real_shape() -> None:
    # Documents that the Pydantic schema accepts the actual ai_monitor
    # --json structure (verified against ai_monitor/ui.py:render_json).
    raw = {
        "updated_at": "2026-05-13T08:00:00",
        "providers": [
            {
                "name": "Claude",
                "ok": True,
                "source": "api",
                "data": {
                    "session_percent_left": 73,
                    "weekly_percent_left": 41,
                    "opus_percent_left": None,
                    "primary_reset": None,
                    "secondary_reset": None,
                    "opus_reset": None,
                    "account_email": None,
                    "account_organization": None,
                    "login_method": None,
                },
                "display": {"foo": "bar"},
                "error": None,
            },
        ],
    }

    parsed = AiMonitorSnapshot.model_validate(raw)

    assert parsed.providers[0].name == "Claude"
    assert parsed.providers[0].shortest_window_remaining_pct == 41.0


def test_ai_monitor_schema_vibe_uses_inverted_usage_percent() -> None:
    raw = {
        "updated_at": "2026-05-13T08:00:00",
        "providers": [
            {
                "name": "Vibe",
                "ok": True,
                "source": "api",
                "data": {
                    "usage_percent": 1.08,
                    "reset_at": None,
                    "payg_enabled": None,
                    "start_date": None,
                    "end_date": None,
                },
                "display": {},
                "error": None,
            },
        ],
    }

    parsed = AiMonitorSnapshot.model_validate(raw)

    assert parsed.providers[0].shortest_window_remaining_pct == pytest.approx(
        98.92, rel=1e-3
    )


@pytest.mark.skipif(shutil.which("python3") is None, reason="python3 not on PATH")
def test_default_fetch_smoke_runs_without_raising() -> None:
    # End-to-end smoke: the default fetcher shells out to ai_monitor.
    # We accept either a dict (ai_monitor installed) or None (not installed,
    # subprocess errored, or output unparseable) — both are valid outcomes.
    router = QuotaRouter()
    result = router._default_fetch()

    assert result is None or isinstance(result, dict)
