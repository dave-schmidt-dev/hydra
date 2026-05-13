"""Pydantic schema for ai_monitor's --json output.

Validated against ai_monitor/ui.py:render_json and ai_monitor/parsing.py.
The schema is intentionally permissive about provider-specific fields
inside ``data`` — providers expose different percent-left keys depending
on which window(s) they report — but strict about the envelope shape so
that PM-7 (schema-mismatch fallback) triggers when ai_monitor's output
contract changes upstream.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

# WHY: ai_monitor emits provider names capitalized (Claude/Codex/Gemini/...),
# but hydra ModelSpec.cli is lowercase. The router does the case-fold mapping
# at lookup time, not here, so this schema preserves ai_monitor's spelling.
_PROVIDER_PCT_KEYS: dict[str, tuple[str, ...]] = {
    "Claude": ("session_percent_left", "weekly_percent_left", "opus_percent_left"),
    "Codex": ("five_hour_percent_left", "weekly_percent_left"),
    "Gemini": ("flash_percent_left", "pro_percent_left"),
    "Copilot": ("premium_percent_left",),
    "Cursor": ("credit_percent_left",),
}


class ProviderEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    ok: bool
    source: str
    data: dict[str, Any] | None = None
    error: str | None = None

    @property
    def shortest_window_remaining_pct(self) -> float:
        """Return the worst-case (lowest) remaining-percent value across the
        provider's known windows.

        WHY shortest window: per plan Section 4.5.3, the binding constraint
        is whichever window runs out first. A provider with 80% of its 5-hour
        budget left but only 5% of its weekly budget left should still be
        routed against as if it had 5% — picking the leader by 5h alone
        would burn through the weekly cap.
        """
        if not self.ok or self.data is None:
            return 0.0

        keys = _PROVIDER_PCT_KEYS.get(self.name, ())
        values: list[float] = []
        for key in keys:
            raw = self.data.get(key)
            if isinstance(raw, (int, float)):
                values.append(float(raw))

        # Vibe: ai_monitor reports usage_percent (% USED), not remaining.
        # See ai_monitor README: "If Mistral shows 1.08% used, AI Monitor
        # will render about 99% remaining after rounding."
        if self.name == "Vibe":
            raw = self.data.get("usage_percent")
            if isinstance(raw, (int, float)):
                values.append(max(0.0, 100.0 - float(raw)))

        if not values:
            return 0.0
        return min(values)


class AiMonitorSnapshot(BaseModel):
    model_config = ConfigDict(extra="allow")

    updated_at: str
    providers: list[ProviderEntry]

    def by_provider_lower(self) -> dict[str, ProviderEntry]:
        """Map lowercased provider name → entry, for matching ModelSpec.cli."""
        return {p.name.lower(): p for p in self.providers}
