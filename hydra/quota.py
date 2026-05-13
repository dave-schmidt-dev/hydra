"""Quota router: pick the best model per tier given current ai_monitor data.

Plan Section 4.5.3:
  - Wrap ai_monitor as a subprocess (``python3 -m ai_monitor --json --once``).
  - Cache snapshots for 60s to avoid hammering provider APIs.
  - Maintain a 60s per-provider blacklist driven by mid-flight 429s.
  - Route by *shortest-window* remaining percentage so that providers don't
    silently exhaust a longer window.
  - When ai_monitor is unavailable or returns a schema we can't parse,
    fall back to round-robin among the tier's candidates (PM-7).
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import ValidationError

from hydra.ai_monitor_schema import AiMonitorSnapshot
from hydra.models import ModelSpec, Tier, default_tiers

logger = logging.getLogger("hydra.quota")

CACHE_TTL_SECONDS = 60.0
BLACKLIST_SECONDS = 60.0
LOW_QUOTA_THRESHOLD_PCT = 5.0
FETCH_TIMEOUT_S = 5.0


class NoCandidateModelError(RuntimeError):
    """All providers in the requested tier are blacklisted or unavailable."""


@dataclass
class QuotaRouter:
    tiers: dict[Tier, list[ModelSpec]] = field(default_factory=default_tiers)
    cli_available: set[str] | None = None
    cache_ttl_seconds: float = CACHE_TTL_SECONDS
    blacklist_seconds: float = BLACKLIST_SECONDS
    fetch: Callable[[], dict | None] | None = None
    clock: Callable[[], float] = time.monotonic
    on_event: Callable[[str, dict], None] | None = None

    _cache: AiMonitorSnapshot | None = field(default=None, init=False, repr=False)
    _cache_ts: float = field(default=0.0, init=False, repr=False)
    _cache_valid: bool = field(default=False, init=False, repr=False)
    _blacklist: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _rr_indexes: dict[Tier, int] = field(default_factory=dict, init=False, repr=False)
    _seen_events: set[str] = field(default_factory=set, init=False, repr=False)

    def pick_model(self, tier: Tier) -> ModelSpec:
        self._prune_blacklist()
        candidates = self._candidates(tier)
        if not candidates:
            raise NoCandidateModelError(
                f"no available candidates for tier {tier!r}: "
                f"all providers blacklisted or filtered out"
            )

        snapshot = self._get_snapshot()
        if snapshot is None:
            return self._round_robin(tier, candidates)

        return self._pick_by_quota(tier, candidates, snapshot)

    def mark_blacklisted(self, provider: str) -> None:
        self._blacklist[provider.lower()] = self.clock()

    def is_blacklisted(self, provider: str) -> bool:
        self._prune_blacklist()
        return provider.lower() in self._blacklist

    def _candidates(self, tier: Tier) -> list[ModelSpec]:
        specs = self.tiers.get(tier, [])
        result: list[ModelSpec] = []
        for spec in specs:
            if self.cli_available is not None and spec.cli not in self.cli_available:
                continue
            if spec.cli in self._blacklist:
                continue
            result.append(spec)
        return result

    def _prune_blacklist(self) -> None:
        # WHY: prune before reads, not on writes — keeps mark_blacklisted O(1)
        # and concentrates the time-dependent check at one query site.
        now = self.clock()
        expired = [
            name
            for name, ts in self._blacklist.items()
            if now - ts >= self.blacklist_seconds
        ]
        for name in expired:
            del self._blacklist[name]

    def _get_snapshot(self) -> AiMonitorSnapshot | None:
        now = self.clock()
        if self._cache_valid and (now - self._cache_ts) < self.cache_ttl_seconds:
            return self._cache

        raw = self._invoke_fetch()
        if raw is None:
            self._emit_once("ai_monitor_unavailable", {})
            self._cache = None
            self._cache_valid = False
            self._cache_ts = now
            return None

        try:
            parsed = AiMonitorSnapshot.model_validate(raw)
        except ValidationError as exc:
            self._emit_once("schema_mismatch", {"error": str(exc)})
            self._cache = None
            self._cache_valid = False
            self._cache_ts = now
            return None

        self._cache = parsed
        self._cache_valid = True
        self._cache_ts = now
        return parsed

    def _invoke_fetch(self) -> dict | None:
        fetcher = self.fetch if self.fetch is not None else self._default_fetch
        try:
            return fetcher()
        except Exception as exc:
            logger.warning("quota router fetch raised: %s", exc)
            self._emit_once("ai_monitor_unavailable", {"error": str(exc)})
            return None

    def _default_fetch(self) -> dict | None:
        try:
            proc = subprocess.run(
                ["python3", "-m", "ai_monitor", "--json", "--once"],
                capture_output=True,
                text=True,
                timeout=FETCH_TIMEOUT_S,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            self._emit_once("ai_monitor_unavailable", {"error": str(exc)})
            return None

        if proc.returncode != 0:
            self._emit_once(
                "ai_monitor_unavailable",
                {"returncode": proc.returncode, "stderr": proc.stderr[-500:]},
            )
            return None

        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            self._emit_once("ai_monitor_unavailable", {"error": str(exc)})
            return None

    def _pick_by_quota(
        self,
        tier: Tier,
        candidates: list[ModelSpec],
        snapshot: AiMonitorSnapshot,
    ) -> ModelSpec:
        entries = snapshot.by_provider_lower()
        scored: list[tuple[float, int, ModelSpec]] = []
        for idx, spec in enumerate(candidates):
            entry = entries.get(spec.cli)
            pct = entry.shortest_window_remaining_pct if entry is not None else 0.0
            scored.append((pct, idx, spec))

        # Stable highest-pct-first by (-pct, original_order).
        scored.sort(key=lambda t: (-t[0], t[1]))
        best_pct, _, chosen = scored[0]

        if best_pct < LOW_QUOTA_THRESHOLD_PCT:
            self._emit(
                "quota_low",
                {
                    "tier": tier,
                    "best_pct": best_pct,
                    "chosen": chosen.to_id(),
                },
            )

        return chosen

    def _round_robin(self, tier: Tier, candidates: list[ModelSpec]) -> ModelSpec:
        idx = self._rr_indexes.get(tier, 0) % len(candidates)
        self._rr_indexes[tier] = (idx + 1) % len(candidates)
        return candidates[idx]

    def _emit(self, event_type: str, payload: dict) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event_type, payload)
        except Exception:
            logger.exception("quota router on_event hook raised")

    def _emit_once(self, event_type: str, payload: dict) -> None:
        if event_type in self._seen_events:
            return
        self._seen_events.add(event_type)
        self._emit(event_type, payload)
