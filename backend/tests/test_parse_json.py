"""Unit tests for ``app.ai.gateway._parse_json``.

Covers the LLM-output JSON parser used by ``Gateway.run``:
- a plain JSON object parses to an equal ``dict``;
- leading/trailing/internal whitespace is tolerated (``json.loads`` semantics);
- the returned value is a real ``dict`` (not a list/scalar);
- invalid / empty / garbage input raises ``SchemaValidationError``;
- valid JSON that is *not* an object (array, scalar, null) raises
  ``SchemaValidationError`` with the "not an object" message;
- the raised error is a ``TerminalProviderError`` subclass (non-retryable).

CHARACTERIZATION NOTE: the live ``_parse_json`` is a *strict* ``json.loads``
plus an ``isinstance(obj, dict)`` check. It performs NO Markdown-fence or
prose stripping. Therefore a ```json fenced block, a bare ``` fenced block,
or JSON wrapped in prose all RAISE ``SchemaValidationError`` rather than being
unwrapped. These tests assert that actual (raising) behavior and flag the gap
between it and a "tolerant" parser. Any fence/prose tolerance, if desired,
must live in the caller, not here.
"""

from __future__ import annotations

import pytest

from app.ai.gateway import (
    SchemaValidationError,
    TerminalProviderError,
    _parse_json,
)


def test_plain_json_object_parses_to_equal_dict() -> None:
    result = _parse_json('{"finding": "ok", "count": 3, "nested": {"a": [1, 2]}}')
    assert result == {"finding": "ok", "count": 3, "nested": {"a": [1, 2]}}
    assert isinstance(result, dict)


def test_empty_object_parses() -> None:
    assert _parse_json("{}") == {}


def test_surrounding_whitespace_is_tolerated() -> None:
    # json.loads ignores leading/trailing whitespace, including newlines/tabs.
    result = _parse_json('  \n\t {"a": 1}  \n ')
    assert result == {"a": 1}


def test_internal_whitespace_and_unicode_preserved() -> None:
    result = _parse_json('{\n  "name": "Acme — Co",\n  "n": 1\n}')
    assert result == {"name": "Acme — Co", "n": 1}


def test_json_fenced_block_raises() -> None:
    # ```json ... ``` is NOT stripped by _parse_json -> invalid JSON.
    fenced = '```json\n{"a": 1}\n```'
    with pytest.raises(SchemaValidationError) as exc:
        _parse_json(fenced)
    assert "not valid JSON" in str(exc.value)


def test_bare_fenced_block_raises() -> None:
    fenced = '```\n{"a": 1}\n```'
    with pytest.raises(SchemaValidationError):
        _parse_json(fenced)


def test_leading_and_trailing_prose_raises() -> None:
    with pytest.raises(SchemaValidationError):
        _parse_json('Here is the result: {"a": 1}\nThanks!')


def test_empty_string_raises() -> None:
    with pytest.raises(SchemaValidationError) as exc:
        _parse_json("")
    assert "not valid JSON" in str(exc.value)


def test_whitespace_only_string_raises() -> None:
    with pytest.raises(SchemaValidationError):
        _parse_json("   \n\t ")


def test_garbage_text_raises() -> None:
    with pytest.raises(SchemaValidationError):
        _parse_json("not json at all {{{")


def test_truncated_json_raises() -> None:
    with pytest.raises(SchemaValidationError):
        _parse_json('{"a": 1')


def test_valid_json_array_raises_not_an_object() -> None:
    # Valid JSON, but a list -> distinct "was not an object" message.
    with pytest.raises(SchemaValidationError) as exc:
        _parse_json("[1, 2, 3]")
    assert "not an object" in str(exc.value)


@pytest.mark.parametrize(
    "scalar", ["42", "3.14", '"a string"', "true", "false", "null"]
)
def test_valid_json_scalars_raise_not_an_object(scalar: str) -> None:
    with pytest.raises(SchemaValidationError) as exc:
        _parse_json(scalar)
    assert "not an object" in str(exc.value)


def test_error_is_terminal_provider_error_subclass() -> None:
    # Schema failures must be non-retryable: SchemaValidationError is terminal.
    assert issubclass(SchemaValidationError, TerminalProviderError)
    with pytest.raises(TerminalProviderError):
        _parse_json("garbage")
