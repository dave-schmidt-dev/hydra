"""Cross-project contract test (plan SR-2).

Verifies Hydra's :data:`hydra.tailer.KNOWN_EVENT_TYPES` is a SUPERSET of
every event type documented in Scarecrow's README. Forward-compatibility
lives in the tailer (unknown event types are silently ignored), but we
still want to catch drift early: when Scarecrow adds a new event type,
we want a fast signal so we can wire any new handling explicitly.

If Scarecrow's README is unreachable on this machine, the test SKIPS
(this is a soft contract test - not every developer has Scarecrow
checked out locally).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hydra.tailer import KNOWN_EVENT_TYPES

SCARECROW_README_PATHS: tuple[Path, ...] = (
    Path.home() / "Documents" / "Projects" / "Scarecrow" / "README.md",
)

# Documented Scarecrow event-type strings are lowercase snake_case
# identifiers. The parser rejects anything else.
_EVENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# "Event types:" sentence pattern - the canonical list lives in a single
# sentence in Scarecrow's README, e.g.:
#     Event types: `session_start`, `session_end`, ...
_EVENT_TYPES_SENTENCE_RE = re.compile(
    r"Event types?:\s*(?P<list>(?:`[a-z][a-z0-9_]*`\s*,?\s*)+)",
    flags=re.IGNORECASE,
)

# Inline `name` extraction from the sentence list.
_BACKTICKED_NAME_RE = re.compile(r"`([a-z][a-z0-9_]*)`")

# JSONL example fallback: lines like {"type":"session_start", ...}.
_JSONL_TYPE_RE = re.compile(r'"type"\s*:\s*"([a-z][a-z0-9_]*)"')


def _find_scarecrow_readme() -> Path | None:
    """Locate Scarecrow's README on disk, or None if not present."""
    for candidate in SCARECROW_README_PATHS:
        if candidate.is_file():
            return candidate
    return None


def _parse_event_types_from_readme(text: str) -> set[str]:
    """Extract event-type strings from Scarecrow's README.

    Strategy (in order of preference):

    1. Find a sentence of the form ``Event types: `a`, `b`, `c`, ...``
       and parse the backtick-wrapped names. This is the canonical
       single-source-of-truth in Scarecrow's README as of v1.5.x.
    2. Fall back to scanning JSONL code-block examples for
       ``"type":"<name>"`` strings.

    Returns the union of names discovered by any strategy. May be empty
    if Scarecrow's README has drifted to a structure neither strategy
    recognises - the contract test then SKIPS with a clear message
    telling the maintainer the parser needs updating.
    """
    found: set[str] = set()

    sentence_match = _EVENT_TYPES_SENTENCE_RE.search(text)
    if sentence_match:
        for name in _BACKTICKED_NAME_RE.findall(sentence_match.group("list")):
            found.add(name)

    for name in _JSONL_TYPE_RE.findall(text):
        found.add(name)

    # Filter out anything that doesn't look like a snake_case event type.
    return {n for n in found if _EVENT_NAME_RE.match(n)}


# Module-level discovery so individual tests can each report cleanly.
_README_PATH = _find_scarecrow_readme()


def test_scarecrow_readme_exists() -> None:
    """Locate Scarecrow's README; SKIP the suite if not found."""
    if _README_PATH is None:
        pytest.skip(
            "Scarecrow README not found at any of: "
            + ", ".join(str(p) for p in SCARECROW_README_PATHS)
            + ". Contract test requires a local Scarecrow checkout."
        )
    assert _README_PATH.is_file()


def test_parser_extracts_events() -> None:
    """The parser must extract a non-empty set of events."""
    if _README_PATH is None:
        pytest.skip("Scarecrow README not present on this machine.")
    text = _README_PATH.read_text(encoding="utf-8")
    documented = _parse_event_types_from_readme(text)
    assert documented, (
        "Parser extracted zero event types from Scarecrow's README. "
        "The README structure may have drifted - update the parser in "
        "tests/test_scarecrow_contract.py to match the new format."
    )


def test_hydra_known_events_covers_scarecrow_documented_events() -> None:
    """Hydra must list every event type Scarecrow documents."""
    if _README_PATH is None:
        pytest.skip("Scarecrow README not present on this machine.")
    text = _README_PATH.read_text(encoding="utf-8")
    documented = _parse_event_types_from_readme(text)
    if not documented:
        pytest.skip(
            "Parser extracted zero events - cannot run contract check. "
            "Update the parser in tests/test_scarecrow_contract.py."
        )
    missing = documented - KNOWN_EVENT_TYPES
    assert not missing, (
        "Scarecrow documents event types that Hydra's KNOWN_EVENT_TYPES "
        f"does not include: {sorted(missing)}. Add them to "
        "hydra.tailer.KNOWN_EVENT_TYPES and wire any new handling in the "
        "tailer/watcher pipeline as needed."
    )


def test_no_typos_in_known_event_types() -> None:
    """Every event in KNOWN_EVENT_TYPES is lowercase snake_case."""
    bad = {name for name in KNOWN_EVENT_TYPES if not _EVENT_NAME_RE.match(name)}
    assert not bad, (
        f"KNOWN_EVENT_TYPES contains non-snake_case entries: {sorted(bad)}. "
        "Scarecrow event types are lowercase snake_case identifiers - "
        "anything else is a typo."
    )


def test_documented_events_subset_or_equal() -> None:
    """The documented set must be a (non-strict) subset of KNOWN_EVENT_TYPES.

    Hydra may know about future event types Scarecrow has not yet
    released; that is fine. The only failure mode is Hydra missing
    something Scarecrow actively documents.
    """
    if _README_PATH is None:
        pytest.skip("Scarecrow README not present on this machine.")
    text = _README_PATH.read_text(encoding="utf-8")
    documented = _parse_event_types_from_readme(text)
    if not documented:
        pytest.skip(
            "Parser extracted zero events - cannot run subset check. "
            "Update the parser in tests/test_scarecrow_contract.py."
        )
    assert documented <= KNOWN_EVENT_TYPES, (
        "Documented Scarecrow events are not a subset of "
        f"KNOWN_EVENT_TYPES. Extra in documented: "
        f"{sorted(documented - KNOWN_EVENT_TYPES)}"
    )
