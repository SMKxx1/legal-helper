"""Expiration extractor — golden request shape + strict-output validation matrix (PLAN §3.8).

NO network: every call rides ``httpx.MockTransport``. These pin the benchmark contract EXACTLY
(n8n ``3epVP6vj2pPbxDdB`` ``Encode PDF`` geminiBody): native-PDF file part, ``file-parser`` plugin,
ZDR + ``google-vertex`` provider pin, the withheld ``document.pdf`` filename, the verbatim 3-step
prompt, and the ``YYYY-MM-DD | ERROR`` output rule validated by ``/^\\d{4}-\\d{2}-\\d{2}$/``.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.config import Settings
from app.expiration.extractor import (
    EXPIRATION_MAX_TOKENS,
    EXPIRATION_PROMPT,
    ExpirationUnavailable,
    build_expiration_request,
    extract_expiration,
    is_iso_date,
)

PDF = b"%PDF-1.4\nfake signed NDA bytes\n%%EOF"


def mk_settings(**kw) -> Settings:
    base: dict = {"openrouter_api_key": "sk-or-test"}
    base.update(kw)
    return Settings(_env_file=None, **base)


def capture(responses: list):
    """MockTransport handler serving ``responses`` in order (last repeats), recording requests.
    dict -> 200 JSON; Exception instance -> raised; httpx.Response -> returned; callable -> called."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = responses[min(len(seen) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        if callable(item):
            return item()
        return httpx.Response(200, json=item)

    return handler, seen


def or_body(
    content: str = "2027-01-15",
    usage: dict | None = None,
    model: str = "google/gemini-3.5-flash",
) -> dict:
    body: dict = {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def run(responses: list, *, settings: Settings | None = None, pdf: bytes = PDF):
    handler, seen = capture(responses)
    settings = settings or mk_settings()
    result = extract_expiration(
        pdf, settings=settings, transport=httpx.MockTransport(handler)
    )
    return result, seen


# --------------------------------------------------------------------------- #
# Golden request shape — the benchmark contract, byte-for-byte
# --------------------------------------------------------------------------- #
def test_request_is_the_benchmark_geminibody() -> None:
    result, seen = run(
        [or_body("2027-01-15", usage={"prompt_tokens": 500, "completion_tokens": 4})]
    )
    assert result.date == "2027-01-15"
    assert len(seen) == 1
    req = seen[0]
    assert req.url.path.endswith("/chat/completions")
    assert req.headers["authorization"] == "Bearer sk-or-test"
    body = json.loads(req.content.decode())

    assert body["model"] == "google/gemini-3.5-flash"
    assert body["max_tokens"] == EXPIRATION_MAX_TOKENS == 1000
    # ZDR fail-closed + the google-vertex pin, EXACTLY the benchmark provider block.
    assert body["provider"] == {
        "data_collection": "deny",
        "zdr": True,
        "allow_fallbacks": False,
        "only": ["google-vertex"],
    }
    assert body["reasoning"] == {"effort": "low", "exclude": True}
    assert body["usage"] == {"include": True}
    assert body["plugins"] == [{"id": "file-parser", "pdf": {"engine": "native"}}]

    content = body["messages"][0]["content"]
    assert body["messages"][0]["role"] == "user"
    # Part 0 = the verbatim 3-step prompt; part 1 = the native-PDF file part.
    assert content[0] == {"type": "text", "text": EXPIRATION_PROMPT}
    file_part = content[1]
    assert file_part["type"] == "file"
    # The real filename is WITHHELD — always the generic document.pdf (anti-cheat).
    assert file_part["file"]["filename"] == "document.pdf"
    data_uri = file_part["file"]["file_data"]
    assert data_uri.startswith("data:application/pdf;base64,")
    # The base64 payload round-trips to the exact PDF bytes we passed in.
    assert base64.b64decode(data_uri.split(",", 1)[1]) == PDF


def test_prompt_guards_the_survival_period_trap() -> None:
    # The Step-3 trap (never use the confidentiality SURVIVAL period) must be in the shipped prompt.
    assert "IGNORE the confidentiality survival period" in EXPIRATION_PROMPT
    assert "YYYY-MM-DD" in EXPIRATION_PROMPT
    assert "respond exactly ERROR" in EXPIRATION_PROMPT


def test_blank_provider_pin_keeps_zdr_but_drops_only() -> None:
    # A blanked pin degrades to any ZDR-qualifying route (documented), never drops ZDR itself.
    _, seen = run([or_body()], settings=mk_settings(expiration_provider_only=""))
    body = json.loads(seen[0].content.decode())
    assert body["provider"] == {
        "data_collection": "deny",
        "zdr": True,
        "allow_fallbacks": False,
    }
    assert "only" not in body["provider"]


def test_zdr_disabled_and_no_pin_omits_provider_block() -> None:
    _, seen = run(
        [or_body()],
        settings=mk_settings(openrouter_zdr_only=False, expiration_provider_only=""),
    )
    body = json.loads(seen[0].content.decode())
    assert "provider" not in body


def test_model_alias_is_configurable() -> None:
    _, seen = run(
        [or_body()],
        settings=mk_settings(openrouter_model_expiration="google/gemini-4-flash"),
    )
    body = json.loads(seen[0].content.decode())
    assert body["model"] == "google/gemini-4-flash"


def test_pure_builder_matches_over_the_wire() -> None:
    # The pure builder and the adapter emit the same body (the adapter just wraps it in a POST).
    built = build_expiration_request(
        PDF, model="google/gemini-3.5-flash", provider_only=("google-vertex",)
    )
    _, seen = run([or_body()])
    wire = json.loads(seen[0].content.decode())
    assert built == wire


# --------------------------------------------------------------------------- #
# Strict output contract — YYYY-MM-DD | ERROR
# --------------------------------------------------------------------------- #
def test_iso_date_accepted() -> None:
    result, _ = run([or_body("2025-03-15")])
    assert result.date == "2025-03-15"
    assert result.status == "ok"


def test_reply_is_trimmed_before_validation() -> None:
    result, _ = run([or_body("  2027-12-31 \n")])
    assert result.date == "2027-12-31"
    assert result.status == "ok"


def test_literal_error_is_ok_with_no_date() -> None:
    result, _ = run([or_body("ERROR")])
    assert result.date is None
    assert result.status == "ok"  # a legitimate "indeterminable" verdict, not a failure


def test_off_contract_reply_is_flagged_no_date() -> None:
    result, _ = run([or_body("The agreement expires sometime in 2027.")])
    assert result.date is None
    assert result.status == "error_output"


def test_non_calendar_but_iso_shaped_passes_like_the_benchmark() -> None:
    # Parity: the benchmark regex is shape-only (no calendar validation). 2027-13-45 is "ISO-shaped".
    assert is_iso_date("2027-13-45")
    result, _ = run([or_body("2027-13-45")])
    assert result.date == "2027-13-45"


# --------------------------------------------------------------------------- #
# Fail-soft transport handling (benchmark neverError posture)
# --------------------------------------------------------------------------- #
def test_connection_error_is_call_failed_not_raised() -> None:
    result, _ = run([httpx.ConnectError("boom")])
    assert result.status == "call_failed"
    assert result.date is None


def test_timeout_is_call_failed() -> None:
    result, _ = run([httpx.ReadTimeout("slow")])
    assert result.status == "call_failed"
    assert result.date is None


def test_non_200_is_call_failed() -> None:
    result, _ = run(
        [httpx.Response(404, json={"error": {"message": "no route satisfies ZDR"}})]
    )
    assert result.status == "call_failed"
    assert "no route" in result.detail


def test_in_body_error_200_is_call_failed() -> None:
    result, _ = run([{"error": {"code": 502, "message": "upstream boom"}}])
    assert result.status == "call_failed"


def test_undecodable_200_is_call_failed() -> None:
    result, _ = run([httpx.Response(200, content=b"not json")])
    assert result.status == "call_failed"


# --------------------------------------------------------------------------- #
# Usage mapping
# --------------------------------------------------------------------------- #
def test_usage_fields_captured() -> None:
    usage = {
        "prompt_tokens": 1234,
        "completion_tokens": 6,
        "completion_tokens_details": {"reasoning_tokens": 40},
        "cost": 0.00042,
    }
    result, _ = run([or_body("2027-01-01", usage=usage)])
    assert result.usage == {
        "prompt_tokens": 1234,
        "completion_tokens": 6,
        "reasoning_tokens": 40,
        "cost_usd": 0.00042,
    }


# --------------------------------------------------------------------------- #
# Capability gate (fail soft)
# --------------------------------------------------------------------------- #
def test_no_key_raises_unavailable() -> None:
    with pytest.raises(ExpirationUnavailable):
        extract_expiration(PDF, settings=mk_settings(openrouter_api_key=""))


def test_registry_disabled_raises_unavailable() -> None:
    from app.capabilities import build_registry

    # A registry built from a keyless settings marks llm_inference disabled.
    registry = build_registry(mk_settings(openrouter_api_key=""))
    with pytest.raises(ExpirationUnavailable):
        extract_expiration(PDF, settings=mk_settings(), registry=registry)
