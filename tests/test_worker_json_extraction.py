"""Tests for hydra.json_extract — noisy-stdout JSON extraction (Phase 4 Task 4.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hydra.json_extract import JSONExtractError, extract_all, extract_json

FIXTURES = Path(__file__).parent / "fixtures" / "model_outputs"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestStaticFixtures:
    def test_clean_object(self) -> None:
        result = extract_json(_load("clean_object.txt"))
        assert isinstance(result, dict)
        assert result["answer"] == "Foo is true."
        assert result["citations"] == []

    def test_clean_array(self) -> None:
        result = extract_json(_load("clean_array.txt"))
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0] == {"id": 1, "label": "alpha"}

    def test_preamble_prose(self) -> None:
        result = extract_json(_load("with_preamble.txt"))
        assert isinstance(result, dict)
        assert result["answer"] == "Bar is also true."
        assert result["citations"][0]["url"] == "https://example.com"

    def test_code_fences(self) -> None:
        result = extract_json(_load("with_fences.txt"))
        assert isinstance(result, list)
        assert result[1]["label"] == "second"

    def test_suffix_prose(self) -> None:
        result = extract_json(_load("with_suffix.txt"))
        assert isinstance(result, dict)
        assert result["answer"] == "Baz might be the case."

    def test_tool_call_noise_returns_first(self) -> None:
        result = extract_json(_load("with_tool_call_noise.txt"))
        assert isinstance(result, dict)
        assert result["query"] == "hydra docs"

    def test_tool_call_noise_extract_all(self) -> None:
        results = extract_all(_load("with_tool_call_noise.txt"))
        assert len(results) == 2
        assert isinstance(results[0], dict)
        assert results[0]["query"] == "hydra docs"
        assert isinstance(results[1], list)
        assert results[1][1]["title"] == "Hydra HISTORY"

    def test_deeply_nested_with_embedded_braces(self) -> None:
        result = extract_json(_load("deeply_nested.txt"))
        assert isinstance(result, dict)
        assert "{brace}-containing" in result["answer"]
        assert len(result["citations"]) == 2
        assert (
            result["citations"][0]["snippet"] == 'She said: "go { left } at the fork"'
        )
        assert result["citations"][1]["snippet"] == 'Config: { "port": 4125 }'


class TestErrorCases:
    def test_no_json_raises(self) -> None:
        with pytest.raises(JSONExtractError):
            extract_json("Just some prose, no JSON here.")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(JSONExtractError):
            extract_json("")

    def test_truncated_json_raises(self) -> None:
        with pytest.raises(JSONExtractError):
            extract_json('Here\'s: {"a": 1, ')

    def test_extract_all_no_json_returns_empty(self) -> None:
        assert extract_all("Just prose.") == []


class TestFallback:
    def test_first_candidate_fails_falls_back(self) -> None:
        result = extract_json('garbage {not json} actually {"a": 1}')
        assert result == {"a": 1}

    def test_multiple_invalid_candidates_then_valid(self) -> None:
        text = "{broken} {also broken} [1, 2, 3]"
        result = extract_json(text)
        assert result == [1, 2, 3]


class TestStringEscaping:
    def test_escaped_backslash_in_string(self) -> None:
        result = extract_json(r'{"path": "C:\\Users\\dave"}')
        assert result == {"path": r"C:\Users\dave"}

    def test_embedded_escaped_quote_doesnt_terminate(self) -> None:
        result = extract_json(r'{"quote": "he said \"hi\""}')
        assert result == {"quote": 'he said "hi"'}

    def test_braces_inside_string_do_not_close_object(self) -> None:
        result = extract_json('{"text": "this has } and { in it", "ok": true}')
        assert result == {"text": "this has } and { in it", "ok": True}

    def test_brackets_inside_string_do_not_close_array(self) -> None:
        result = extract_json('["has ] and [ inside", "second"]')
        assert result == ["has ] and [ inside", "second"]


class TestObjectVsArrayTie:
    def test_object_with_array_value(self) -> None:
        result = extract_json('{"a": []}')
        assert result == {"a": []}

    def test_array_with_object_inside(self) -> None:
        result = extract_json('[{"a": 1}]')
        assert result == [{"a": 1}]


class TestMatchingBraceTypes:
    def test_mismatched_open_does_not_falsely_close(self) -> None:
        # A '}' should not close a '[' — the scanner must track matched types.
        result = extract_json("[1, 2, 3]")
        assert result == [1, 2, 3]

    def test_nested_mixed_brackets(self) -> None:
        result = extract_json('{"items": [1, [2, 3], {"x": [4]}]}')
        assert result == {"items": [1, [2, 3], {"x": [4]}]}


class TestExtractAll:
    def test_returns_in_document_order(self) -> None:
        text = '{"a": 1} then {"b": 2} and finally [3, 4]'
        results = extract_all(text)
        assert results == [{"a": 1}, {"b": 2}, [3, 4]]

    def test_skips_invalid_blobs(self) -> None:
        text = '{not json} {"valid": true} ["also valid"]'
        results = extract_all(text)
        assert results == [{"valid": True}, ["also valid"]]
