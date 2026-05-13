"""Noisy-stdout JSON extraction for LLM CLI worker responses.

LLM CLIs intermix JSON with preamble prose, code fences, tool-call markup, and
suffix prose. This module scans for the first complete JSON object or array
using a brace-depth state machine that correctly handles strings, escapes,
and nested-but-mismatched bracket types.
"""

from __future__ import annotations

import json
import re

__all__ = ["JSONExtractError", "extract_all", "extract_json"]


class JSONExtractError(ValueError):
    """Raised when no valid JSON object/array can be extracted from stdout."""


_FENCE_RE = re.compile(r"```(?:json|javascript)?\s*\n?|\n?```", re.IGNORECASE)


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text)


def _scan_candidate(text: str, start: int) -> int | None:
    """Return the exclusive end index of the JSON value beginning at `start`.

    WHY a hand-rolled state machine: json.loads alone can't locate where a
    JSON value ends inside noisy text. We track string state and bracket type
    so that braces inside strings, and mismatched closers (e.g., '}' on a '['
    stack), don't falsely terminate the scan. None if the value never closes.
    """
    stack: list[str] = []
    in_string = False
    escape = False
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                if not stack:
                    return None
                opener = stack[-1]
                if (opener == "{" and ch != "}") or (opener == "[" and ch != "]"):
                    return None
                stack.pop()
                if not stack:
                    return i + 1
        i += 1
    return None


def _iter_candidates(text: str):
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "{[":
            end = _scan_candidate(text, i)
            if end is not None:
                yield i, end, ch
        i += 1


def extract_json(text: str) -> dict | list:
    """Extract the first complete JSON object or array from noisy text.

    Strips common code-fence markers, then walks the text counting bracket
    depth while tracking string state. The first slice that parses as JSON
    wins. Falls forward through invalid slices.
    """
    cleaned = _strip_fences(text)
    for start, end, _opener in _iter_candidates(cleaned):
        slice_ = cleaned[start:end]
        try:
            return json.loads(slice_)
        except json.JSONDecodeError:
            continue
    raise JSONExtractError("no parseable JSON value found in text")


def extract_all(text: str) -> list[dict | list]:
    """Extract all complete JSON values in document order.

    Skips invalid slices, advances past each successfully parsed value so that
    inner objects aren't re-emitted as standalone results.
    """
    cleaned = _strip_fences(text)
    results: list[dict | list] = []
    i = 0
    n = len(cleaned)
    while i < n:
        ch = cleaned[i]
        if ch in "{[":
            end = _scan_candidate(cleaned, i)
            if end is not None:
                slice_ = cleaned[i:end]
                try:
                    results.append(json.loads(slice_))
                    i = end
                    continue
                except json.JSONDecodeError:
                    pass
        i += 1
    return results
