"""Tests for hydra.citations — validates worker JSON shape (Phase 4 Task 4.1)."""

from __future__ import annotations

import pytest

from hydra.citations import (
    Citation,
    CitationValidationError,
    ValidatedAnswer,
    validate,
)


class TestHappyPath:
    def test_well_formed_response(self) -> None:
        response = {
            "answer": "Foo [1]. Bar [2]. Baz [3]. Qux [4].",
            "citations": [
                {
                    "id": 1,
                    "source_type": "web",
                    "url": "https://example.com/a",
                    "quoted_snippet": "foo",
                },
                {
                    "id": 2,
                    "source_type": "web",
                    "url": "https://example.com/b",
                    "quoted_snippet": "bar",
                },
                {
                    "id": 3,
                    "source_type": "local",
                    "file_path": "/notes/a.md",
                    "quoted_snippet": "baz",
                },
                {
                    "id": 4,
                    "source_type": "local",
                    "file_path": "/notes/b.md",
                    "quoted_snippet": "qux",
                },
            ],
        }
        result = validate(response)
        assert isinstance(result, ValidatedAnswer)
        assert len(result.citations) == 4
        assert result.unsourced_claims == []
        assert result.warnings == []
        assert "[1]" in result.answer
        assert "[4]" in result.answer

    def test_returns_frozen_citation_dataclass(self) -> None:
        response = {
            "answer": "Foo [1].",
            "citations": [
                {
                    "id": 1,
                    "source_type": "web",
                    "url": "https://example.com",
                    "quoted_snippet": "foo",
                }
            ],
        }
        result = validate(response)
        c = result.citations[0]
        assert isinstance(c, Citation)
        assert c.id == 1
        assert c.source_type == "web"
        assert c.url == "https://example.com"
        assert c.file_path is None


class TestMissingFields:
    def test_missing_url_on_web_citation(self) -> None:
        response = {
            "answer": "Foo [1]. Bar [2].",
            "citations": [
                {"id": 1, "source_type": "web", "quoted_snippet": "foo"},
                {
                    "id": 2,
                    "source_type": "web",
                    "url": "https://example.com",
                    "quoted_snippet": "bar",
                },
            ],
        }
        result = validate(response)
        assert len(result.citations) == 1
        assert result.citations[0].id == 2
        assert "[1]" not in result.answer
        assert "[2]" in result.answer
        assert any("1" in w for w in result.warnings)

    def test_empty_url_on_web_citation(self) -> None:
        response = {
            "answer": "Foo [1].",
            "citations": [
                {"id": 1, "source_type": "web", "url": "", "quoted_snippet": "foo"},
            ],
        }
        result = validate(response)
        assert result.citations == []
        assert "[1]" not in result.answer
        assert len(result.warnings) >= 1

    def test_missing_file_path_on_local_citation(self) -> None:
        response = {
            "answer": "Foo [1].",
            "citations": [
                {"id": 1, "source_type": "local", "quoted_snippet": "foo"},
            ],
        }
        result = validate(response)
        assert result.citations == []
        assert "[1]" not in result.answer
        assert len(result.warnings) >= 1

    def test_missing_quoted_snippet(self) -> None:
        response = {
            "answer": "Foo [1].",
            "citations": [
                {"id": 1, "source_type": "web", "url": "https://example.com"},
            ],
        }
        result = validate(response)
        assert result.citations == []
        assert "[1]" not in result.answer
        assert len(result.warnings) >= 1

    def test_missing_id(self) -> None:
        response = {
            "answer": "Foo.",
            "citations": [
                {
                    "source_type": "web",
                    "url": "https://example.com",
                    "quoted_snippet": "foo",
                },
            ],
        }
        result = validate(response)
        assert result.citations == []
        assert len(result.warnings) >= 1


class TestUnknownSourceType:
    def test_unknown_source_type_dropped(self) -> None:
        response = {
            "answer": "Foo [3].",
            "citations": [
                {"id": 3, "source_type": "tweetstorm", "quoted_snippet": "foo"},
            ],
        }
        result = validate(response)
        assert result.citations == []
        assert "[3]" not in result.answer
        assert any("tweetstorm" in w or "3" in w for w in result.warnings)


class TestInlineRefs:
    def test_ref_to_nonexistent_id_stripped(self) -> None:
        response = {
            "answer": "Foo [99] bar.",
            "citations": [],
        }
        result = validate(response)
        assert "[99]" not in result.answer
        assert "Foo" in result.answer
        assert "bar" in result.answer
        assert any("99" in w for w in result.warnings)

    def test_ref_to_dropped_citation_stripped(self) -> None:
        response = {
            "answer": "Foo [1].",
            "citations": [{"id": 1, "source_type": "web", "quoted_snippet": "foo"}],
        }
        result = validate(response)
        assert "[1]" not in result.answer

    def test_empty_citations_no_refs(self) -> None:
        response = {"answer": "Just prose.", "citations": []}
        result = validate(response)
        assert result.answer == "Just prose."
        assert result.citations == []
        assert result.unsourced_claims == []
        assert result.warnings == []

    def test_empty_citations_with_ref(self) -> None:
        response = {"answer": "Foo [1].", "citations": []}
        result = validate(response)
        assert "[1]" not in result.answer
        assert len(result.warnings) >= 1


class TestUnsourced:
    def test_unsourced_citation_keeps_text_lists_claim(self) -> None:
        response = {
            "answer": "Foo is true [1]. Bar is also true [2].",
            "citations": [
                {
                    "id": 1,
                    "source_type": "web",
                    "url": "https://example.com",
                    "quoted_snippet": "foo",
                },
                {
                    "id": 2,
                    "source_type": "unsourced",
                    "quoted_snippet": "Bar is also true.",
                },
            ],
        }
        result = validate(response)
        assert len(result.citations) == 2
        assert result.citations[1].source_type == "unsourced"
        assert result.citations[1].url is None
        assert result.citations[1].file_path is None
        assert "[1]" in result.answer
        assert "[2]" in result.answer
        assert "Bar is also true." in result.unsourced_claims


class TestStructuralErrors:
    def test_answer_not_string_raises(self) -> None:
        with pytest.raises(CitationValidationError):
            validate({"answer": 42, "citations": []})

    def test_citations_not_list_raises(self) -> None:
        with pytest.raises(CitationValidationError):
            validate({"answer": "x", "citations": "nope"})

    def test_missing_answer_raises(self) -> None:
        with pytest.raises(CitationValidationError):
            validate({"citations": []})

    def test_missing_citations_raises(self) -> None:
        with pytest.raises(CitationValidationError):
            validate({"answer": "x"})

    def test_non_dict_raises(self) -> None:
        with pytest.raises(CitationValidationError):
            validate([])  # type: ignore[arg-type]


class TestDuplicates:
    def test_duplicate_ids_keep_first(self) -> None:
        response = {
            "answer": "Foo [1].",
            "citations": [
                {
                    "id": 1,
                    "source_type": "web",
                    "url": "https://first.example.com",
                    "quoted_snippet": "first",
                },
                {
                    "id": 1,
                    "source_type": "web",
                    "url": "https://second.example.com",
                    "quoted_snippet": "second",
                },
            ],
        }
        result = validate(response)
        assert len(result.citations) == 1
        assert result.citations[0].url == "https://first.example.com"
        assert any("duplicate" in w.lower() or "1" in w for w in result.warnings)


class TestInvalidIds:
    def test_zero_id_dropped(self) -> None:
        response = {
            "answer": "Foo.",
            "citations": [
                {
                    "id": 0,
                    "source_type": "web",
                    "url": "https://example.com",
                    "quoted_snippet": "foo",
                },
            ],
        }
        result = validate(response)
        assert result.citations == []
        assert len(result.warnings) >= 1

    def test_negative_id_dropped(self) -> None:
        response = {
            "answer": "Foo.",
            "citations": [
                {
                    "id": -1,
                    "source_type": "web",
                    "url": "https://example.com",
                    "quoted_snippet": "foo",
                },
            ],
        }
        result = validate(response)
        assert result.citations == []
        assert len(result.warnings) >= 1

    def test_non_int_id_dropped(self) -> None:
        response = {
            "answer": "Foo.",
            "citations": [
                {
                    "id": "one",
                    "source_type": "web",
                    "url": "https://example.com",
                    "quoted_snippet": "foo",
                },
            ],
        }
        result = validate(response)
        assert result.citations == []
        assert len(result.warnings) >= 1
