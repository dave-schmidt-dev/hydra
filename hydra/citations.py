"""Citation validation for worker LLM responses.

Workers prompt LLMs to emit ``{"answer": str, "citations": [...]}``. This module
enforces the plan-mandated contract: every claim must carry URL+snippet (web) or
file_path+snippet (local). Invalid citations are dropped with warnings; orphan
``[N]`` refs in the answer are stripped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "Citation",
    "CitationValidationError",
    "SourceType",
    "ValidatedAnswer",
    "validate",
]

SourceType = Literal["web", "local", "unsourced"]

_VALID_SOURCE_TYPES = frozenset({"web", "local", "unsourced"})


@dataclass(frozen=True)
class Citation:
    id: int
    source_type: SourceType
    quoted_snippet: str
    url: str | None = None
    file_path: str | None = None


@dataclass(frozen=True)
class ValidatedAnswer:
    answer: str
    citations: list[Citation]
    unsourced_claims: list[str]
    warnings: list[str] = field(default_factory=list)


class CitationValidationError(ValueError):
    """Raised when the response is structurally unsalvageable (e.g., not a dict)."""


def _strip_ref(answer: str, cid: int) -> str:
    # WHY strip orphan [N]: an inline ref to a dropped/missing citation would
    # mislead the reader into thinking the claim is sourced. Clean removal
    # preserves prose; surrounding spaces are normalized to avoid "Foo  bar".
    pattern = re.compile(r"\s*\[" + re.escape(str(cid)) + r"\]")
    return pattern.sub("", answer)


def _validate_one(
    raw: object, seen_ids: set[int], warnings: list[str]
) -> Citation | None:
    if not isinstance(raw, dict):
        warnings.append(f"citation dropped: not a dict ({raw!r})")
        return None

    cid = raw.get("id")
    if not isinstance(cid, int) or isinstance(cid, bool) or cid <= 0:
        warnings.append(f"citation dropped: invalid or missing id ({cid!r})")
        return None

    if cid in seen_ids:
        warnings.append(f"citation dropped: duplicate id {cid}")
        return None

    source_type = raw.get("source_type")
    if source_type not in _VALID_SOURCE_TYPES:
        warnings.append(
            f"citation [{cid}] dropped: unknown source_type {source_type!r}"
        )
        return None

    snippet = raw.get("quoted_snippet")
    if not isinstance(snippet, str) or not snippet:
        warnings.append(f"citation [{cid}] dropped: missing quoted_snippet")
        return None

    url = raw.get("url")
    file_path = raw.get("file_path")

    if source_type == "web":
        if not isinstance(url, str) or not url:
            warnings.append(f"citation [{cid}] dropped: web source missing url")
            return None
        return Citation(
            id=cid, source_type="web", quoted_snippet=snippet, url=url, file_path=None
        )

    if source_type == "local":
        if not isinstance(file_path, str) or not file_path:
            warnings.append(f"citation [{cid}] dropped: local source missing file_path")
            return None
        return Citation(
            id=cid,
            source_type="local",
            quoted_snippet=snippet,
            url=None,
            file_path=file_path,
        )

    return Citation(
        id=cid,
        source_type="unsourced",
        quoted_snippet=snippet,
        url=None,
        file_path=None,
    )


_REF_RE = re.compile(r"\[(\d+)\]")


def validate(response: dict) -> ValidatedAnswer:
    """Validate the worker's structured response.

    Returns a ValidatedAnswer with kept citations, stripped orphan refs,
    flagged unsourced claims, and a warnings list for diagnostics.
    """
    if not isinstance(response, dict):
        raise CitationValidationError(f"response must be a dict, got {type(response)}")

    if "answer" not in response:
        raise CitationValidationError("response missing required 'answer' field")
    if "citations" not in response:
        raise CitationValidationError("response missing required 'citations' field")

    answer = response["answer"]
    if not isinstance(answer, str):
        raise CitationValidationError(f"'answer' must be a string, got {type(answer)}")

    raw_citations = response["citations"]
    if not isinstance(raw_citations, list):
        raise CitationValidationError(
            f"'citations' must be a list, got {type(raw_citations)}"
        )

    warnings: list[str] = []
    citations: list[Citation] = []
    seen_ids: set[int] = set()
    for raw in raw_citations:
        citation = _validate_one(raw, seen_ids, warnings)
        if citation is not None:
            citations.append(citation)
            seen_ids.add(citation.id)

    by_id = {c.id: c for c in citations}
    unsourced_claims: list[str] = []

    for ref_match in _REF_RE.finditer(answer):
        ref_id = int(ref_match.group(1))
        if ref_id not in by_id:
            warnings.append(f"orphan ref [{ref_id}] stripped from answer")

    cleaned_answer = answer
    referenced_ids = {int(m.group(1)) for m in _REF_RE.finditer(answer)}
    for ref_id in referenced_ids:
        if ref_id not in by_id:
            cleaned_answer = _strip_ref(cleaned_answer, ref_id)

    for ref_id in referenced_ids:
        citation = by_id.get(ref_id)
        if citation is not None and citation.source_type == "unsourced":
            unsourced_claims.append(citation.quoted_snippet)

    return ValidatedAnswer(
        answer=cleaned_answer,
        citations=citations,
        unsourced_claims=unsourced_claims,
        warnings=warnings,
    )
