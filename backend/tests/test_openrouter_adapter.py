"""OpenRouterAdapter contract tests — request shape, taxonomy, usage/cost, repair loop, selection.

NO network: every HTTP call rides httpx.MockTransport. These pin the PLAN §3.8/§6 contract:
ZDR fail-closed provider preferences, json_schema structured output with D1 property order
preserved, anthropic/* cache_control passthrough, reasoning-effort mapping, usage accounting with
OpenRouter's reported cost authoritative, the one-repair-then-terminal schema loop, and the
gateway-builder adapter selection (OpenRouter primary / direct-Anthropic fallback).
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.ai.gateway import (
    EFFORTS,
    GatewayRequest,
    RetryableProviderError,
    SchemaValidationError,
    TerminalProviderError,
)
from app.ai.openrouter import (
    OpenRouterAdapter,
    _coerce_omitted_defaults,
    build_openrouter_request,
    validate_instance,
)

# A small portable schema (obeys engine.portable_schema rules) with a D1-meaningful order:
# rationale (reasoning) before severity (verdict).
SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["rationale", "severity", "note"],
    "properties": {
        "rationale": {"type": "string"},
        "severity": {"type": "string", "enum": ["low", "high"]},
        "note": {"type": ["string", "null"]},
    },
}

GOOD_OBJ = {"rationale": "r", "severity": "low", "note": None}
GOOD_TEXT = json.dumps(GOOD_OBJ)


def make_req(**kw) -> GatewayRequest:
    defaults: dict = dict(
        role="rate",
        schema=SCHEMA,
        system="SYSTEM",
        task="TASK",
        stable_blocks=["PLAYBOOK", "DOC"],
        effort="medium",
        max_tokens=512,
    )
    defaults.update(kw)
    return GatewayRequest(**defaults)


def ok_body(
    text: str = GOOD_TEXT,
    usage: dict | None = None,
    model: str = "anthropic/claude-opus-4-8",
    finish: str = "stop",
    native: str | None = None,
) -> dict:
    body: dict = {
        "id": "gen-1",
        "model": model,
        "choices": [
            {
                "finish_reason": finish,
                "native_finish_reason": native,
                "message": {"role": "assistant", "content": text},
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def capture(responses: list):
    """MockTransport handler serving ``responses`` in order (last repeats), recording requests.
    Items: dict -> 200 JSON; callable -> fresh httpx.Response per call (never reuse a Response
    instance across requests); exception instance -> raised."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = responses[min(len(seen) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item()
        return httpx.Response(200, json=item)

    return handler, seen


def make_adapter(
    handler, model: str = "anthropic/claude-opus-4-8", **kw
) -> OpenRouterAdapter:
    return OpenRouterAdapter(
        "sk-or-test", model, transport=httpx.MockTransport(handler), **kw
    )


# --------------------------------------------------------------------------- #
# Request shape
# --------------------------------------------------------------------------- #
def test_request_shape_zdr_json_schema_usage_no_temperature() -> None:
    handler, seen = capture(
        [ok_body(usage={"prompt_tokens": 10, "completion_tokens": 2})]
    )
    adapter = make_adapter(handler, provider_only=("anthropic",), timeout_s=150.0)
    adapter.complete(make_req())

    assert len(seen) == 1
    req = seen[0]
    assert req.url.path == "/api/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-or-test"
    payload = json.loads(req.content.decode())
    # ZDR fail-closed prefs + pinning, all present on the wire.
    assert payload["provider"] == {
        "data_collection": "deny",
        "zdr": True,
        "allow_fallbacks": False,
        "only": ["anthropic"],
    }
    rf = payload["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"] == SCHEMA
    # D1: property order (== decode order) survives serialization untouched.
    assert list(rf["json_schema"]["schema"]["properties"]) == list(SCHEMA["properties"])
    assert payload["usage"] == {"include": True}  # usage accounting requested
    assert payload["max_tokens"] == 512
    assert "temperature" not in payload
    # Timeout comes from construction (settings.provider_timeout_s at the call site).
    assert adapter._client.timeout == httpx.Timeout(150.0)


def test_anthropic_cache_control_blocks_and_prefix_order() -> None:
    body = build_openrouter_request(make_req(), "anthropic/claude-opus-4-8")
    msgs = body["messages"]
    assert [m["role"] for m in msgs] == ["system", "user"]
    blocks = msgs[0]["content"]
    # Stable prefix first, in order; ONE cache breakpoint on the LAST stable block.
    assert [b["text"] for b in blocks] == ["SYSTEM", "PLAYBOOK", "DOC"]
    assert all("cache_control" not in b for b in blocks[:-1])
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}
    assert msgs[1] == {"role": "user", "content": "TASK"}
    # Byte-stable prefix across calls: identical construction -> identical serialized bytes.
    again = build_openrouter_request(make_req(), "anthropic/claude-opus-4-8")
    assert json.dumps(msgs[0]) == json.dumps(again["messages"][0])


def test_cache_ttl_1h_marks_the_breakpoint() -> None:
    body = build_openrouter_request(
        make_req(), "anthropic/claude-opus-4-8", cache_ttl="1h"
    )
    assert body["messages"][0]["content"][-1]["cache_control"] == {
        "type": "ephemeral",
        "ttl": "1h",
    }


def test_non_anthropic_system_is_joined_string_without_cache_control() -> None:
    body = build_openrouter_request(make_req(), "openai/gpt-5-mini")
    assert body["messages"][0]["content"] == "SYSTEM\n\nPLAYBOOK\n\nDOC"


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        ("anthropic/claude-opus-4-8", "high", {"effort": "high"}),
        ("anthropic/claude-opus-4-8", "max", {"effort": "high"}),  # OR tops out at high
        ("anthropic/claude-opus-4-8", "min", {"effort": "low"}),
        ("anthropic/claude-sonnet-4-6", "medium", {"effort": "medium"}),
        ("openai/gpt-5-mini", "medium", {"effort": "medium"}),
    ],
)
def test_reasoning_effort_included_where_supported(model, effort, expected) -> None:
    body = build_openrouter_request(make_req(effort=effort), model)
    assert body["reasoning"] == expected


@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-haiku-4-5",
        "anthropic/claude-sonnet-4-5",
        "mistralai/mistral-large",  # unvalidated family -> omit, run at provider default
    ],
)
def test_reasoning_omitted_where_unsupported(model) -> None:
    assert "reasoning" not in build_openrouter_request(make_req(), model)


def test_zdr_disabled_sends_no_provider_prefs() -> None:
    body = build_openrouter_request(
        make_req(), "anthropic/claude-opus-4-8", zdr_only=False
    )
    assert "provider" not in body


def test_provider_pin_without_zdr_still_disables_fallbacks() -> None:
    body = build_openrouter_request(
        make_req(),
        "anthropic/claude-opus-4-8",
        zdr_only=False,
        provider_only=("anthropic",),
    )
    assert body["provider"] == {"only": ["anthropic"], "allow_fallbacks": False}


def test_zdr_fail_closed() -> None:
    """PLAN §6, the most important test in the repo: EVERY outgoing OpenRouter request body must
    carry the ZDR filters — across every agent's model, every effort, every style — with no code
    path that omits or waters them down while ``zdr_only`` (the adapter's default) is on.

    The guarantee lives in ``zdr`` and ``data_collection``: they are hard filters, so a request
    that no compliant provider can serve returns ``404 No endpoints found matching your data
    policy`` rather than being served by a non-compliant one (that fail-closed behaviour is
    asserted by ``test_no_zdr_route_404_surfaces_as_stable_error_code``).

    ``allow_fallbacks`` is deliberately NOT part of that guarantee: it only decides *which* of the
    surviving compliant providers serves the request. It is on because pinning to a single
    provider — 1 of the 21 serving glm-5.3 under ZDR — meant one busy upstream pool failed the
    whole review while 20 equally-compliant providers sat idle.
    """
    seen_bodies: list[dict] = []
    for model in (
        "z-ai/glm-5.3",
        "z-ai/glm-5.3-flash",
        "anthropic/claude-opus-4.8",
        "openai/gpt-5-mini",
        "google/gemini-2.5-pro",
    ):
        for effort in EFFORTS:
            seen_bodies.append(build_openrouter_request(make_req(effort=effort), model))
    assert seen_bodies  # sanity: the matrix actually ran
    for body in seen_bodies:
        # the compliance half — never absent, never weakened
        assert body["provider"]["zdr"] is True
        assert body["provider"]["data_collection"] == "deny"
        # and nothing may pin the request to a single provider by default
        assert "only" not in body["provider"]

    # The live-call path too, not just the pure builder: complete() must send the same block.
    handler, seen = capture(
        [ok_body(usage={"prompt_tokens": 1, "completion_tokens": 1})]
    )
    make_adapter(handler).complete(make_req())
    payload = json.loads(seen[0].content.decode())
    assert payload["provider"]["zdr"] is True
    assert payload["provider"]["data_collection"] == "deny"


def test_an_explicit_provider_pin_disables_fallbacks() -> None:
    """A pin means THIS provider or nothing — widening it would defeat the point of pinning."""
    body = build_openrouter_request(
        make_req(), "z-ai/glm-5.3", provider_only=("google-vertex",)
    )
    assert body["provider"]["only"] == ["google-vertex"]
    assert body["provider"]["allow_fallbacks"] is False
    assert body["provider"]["zdr"] is True  # a pin never drops the ZDR filter


def test_no_zdr_route_404_surfaces_as_stable_error_code() -> None:
    """A 404 'no endpoints' response (no route satisfies the ZDR/provider preferences) must map to
    the ``no_zdr_route`` error code the review pipeline surfaces to the client — never a silent
    downgrade to a non-ZDR route."""
    from app.ai.gateway import NoZdrRouteError, error_code_for

    handler, _ = capture(
        [
            lambda: httpx.Response(
                404, json={"error": {"code": 404, "message": "no endpoints found"}}
            )
        ]
    )
    with pytest.raises(NoZdrRouteError) as ei:
        make_adapter(handler).complete(make_req())
    assert error_code_for(ei.value) == "no_zdr_route"


def test_strict_false_for_unvalidated_family_but_schema_still_sent() -> None:
    body = build_openrouter_request(make_req(), "mistralai/mistral-large")
    assert body["response_format"]["json_schema"]["strict"] is False
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA


# --------------------------------------------------------------------------- #
# Usage / cost mapping
# --------------------------------------------------------------------------- #
def test_openrouter_cost_is_authoritative_and_cache_details_map() -> None:
    usage = {
        "prompt_tokens": 1000,  # OpenAI shape: INCLUDES cache read+write
        "completion_tokens": 200,
        "cost": 0.0123,
        "prompt_tokens_details": {"cached_tokens": 300},
        "cache_creation_input_tokens": 100,
    }
    handler, _ = capture([ok_body(usage=usage)])
    raw = make_adapter(handler).complete(make_req())
    assert (
        raw.usage.input_tokens == 600
    )  # prompt minus cache read+write (engine semantics)
    assert raw.usage.output_tokens == 200
    assert raw.usage.cache_read_tokens == 300
    assert raw.usage.cache_write_tokens == 100
    assert raw.usage.cost_usd == pytest.approx(0.0123)  # reported, NOT the local table
    assert raw.model_version == "anthropic/claude-opus-4-8"


def test_byok_upstream_inference_cost_is_added() -> None:
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 1,
        "cost": 0.001,  # OpenRouter's BYOK fee
        "cost_details": {"upstream_inference_cost": 0.02},  # the provider-key bill
    }
    handler, _ = capture([ok_body(usage=usage)])
    raw = make_adapter(handler).complete(make_req())
    assert raw.usage.cost_usd == pytest.approx(0.021)


def test_missing_cost_records_zero_not_an_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # There is no local pricing table in this engine — if OpenRouter ever reports no cost at
    # all, record 0 and log a warning rather than estimate one (see app.ai.openrouter._map_usage).
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}  # no "cost" reported
    handler, _ = capture([ok_body(usage=usage)])
    raw = make_adapter(handler).complete(make_req())
    assert raw.usage.cost_usd == 0.0


# --------------------------------------------------------------------------- #
# Error taxonomy
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (408, RetryableProviderError),
        (429, RetryableProviderError),
        (500, RetryableProviderError),
        (502, RetryableProviderError),
        (503, RetryableProviderError),
        (400, TerminalProviderError),
        (401, TerminalProviderError),
        (402, TerminalProviderError),
        (403, TerminalProviderError),
        (
            404,
            TerminalProviderError,
        ),  # incl. "no ZDR route" — fail closed, no downgrade
    ],
)
def test_http_status_taxonomy(status, exc) -> None:
    handler, _ = capture(
        [
            lambda: httpx.Response(
                status, json={"error": {"code": status, "message": "x"}}
            )
        ]
    )
    with pytest.raises(exc):
        make_adapter(handler).complete(make_req())


def test_timeout_and_connect_errors_are_retryable() -> None:
    handler, _ = capture([httpx.ReadTimeout("slow")])
    with pytest.raises(RetryableProviderError):
        make_adapter(handler).complete(make_req())

    handler, _ = capture([httpx.ConnectError("refused")])
    with pytest.raises(RetryableProviderError):
        make_adapter(handler).complete(make_req())


def test_inbody_200_error_maps_like_its_code() -> None:
    handler, _ = capture([{"error": {"code": 429, "message": "provider overloaded"}}])
    with pytest.raises(RetryableProviderError):
        make_adapter(handler).complete(make_req())

    handler, _ = capture([{"error": {"code": 403, "message": "moderation"}}])
    with pytest.raises(TerminalProviderError):
        make_adapter(handler).complete(make_req())


def test_content_filter_finish_reason_is_terminal() -> None:
    handler, _ = capture([ok_body(finish="content_filter")])
    with pytest.raises(TerminalProviderError) as ei:
        make_adapter(handler).complete(make_req())
    assert not isinstance(ei.value, RetryableProviderError)
    assert "filter" in str(ei.value)


def test_native_refusal_is_terminal() -> None:
    # Parity with the direct adapter's stop_reason == "refusal" handling.
    handler, _ = capture([ok_body(native="refusal")])
    with pytest.raises(TerminalProviderError):
        make_adapter(handler).complete(make_req())


def test_undecodable_200_retries_once_then_succeeds() -> None:
    handler, seen = capture(
        [
            lambda: httpx.Response(200, content=b"<html>edge error page</html>"),
            ok_body(usage={"prompt_tokens": 1, "completion_tokens": 1}),
        ]
    )
    raw = make_adapter(handler).complete(make_req())
    assert len(seen) == 2
    assert raw.text == GOOD_TEXT


def test_undecodable_200_twice_is_terminal() -> None:
    handler, seen = capture([lambda: httpx.Response(200, content=b"nope")])
    with pytest.raises(TerminalProviderError):
        make_adapter(handler).complete(make_req())
    assert len(seen) == 2  # exactly one in-place retry


# --------------------------------------------------------------------------- #
# Client-side validation + repair round-trip
# --------------------------------------------------------------------------- #
def test_repair_round_trip_success_and_usage_summed() -> None:
    first = ok_body(
        text="NOT JSON",
        usage={"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.01},
    )
    second = ok_body(
        usage={"prompt_tokens": 120, "completion_tokens": 12, "cost": 0.02}
    )
    handler, seen = capture([first, second])
    raw = make_adapter(handler).complete(make_req())

    assert len(seen) == 2
    first_payload = json.loads(seen[0].content.decode())
    repair_payload = json.loads(seen[1].content.decode())
    msgs = repair_payload["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    # The cached stable prefix is byte-identical on the repair call.
    assert msgs[0] == first_payload["messages"][0]
    assert msgs[2]["content"] == "NOT JSON"  # the invalid reply, echoed back
    assert "not valid JSON" in msgs[3]["content"]  # the validator error, re-prompted
    assert raw.text == GOOD_TEXT
    # Both calls were paid: tokens and costs sum.
    assert raw.usage.input_tokens == 220
    assert raw.usage.output_tokens == 22
    assert raw.usage.cost_usd == pytest.approx(0.03)


def test_repair_then_fail_raises_schema_validation_error() -> None:
    bad = ok_body(
        text=json.dumps({"rationale": "r", "severity": "banana", "note": None})
    )
    handler, seen = capture([bad])  # both attempts return the enum violation
    with pytest.raises(SchemaValidationError):
        make_adapter(handler).complete(make_req())
    assert len(seen) == 2  # exactly ONE repair round-trip, then terminal
    assert "enum" in json.loads(seen[1].content.decode())["messages"][3]["content"]


def test_validate_instance_covers_portable_subset() -> None:
    assert validate_instance(GOOD_OBJ, SCHEMA) is None
    assert (
        validate_instance({**GOOD_OBJ, "note": "text"}, SCHEMA) is None
    )  # union branch
    assert "missing required" in validate_instance({"rationale": "r"}, SCHEMA)
    assert "enum" in validate_instance(
        {"rationale": "r", "severity": "nope", "note": None}, SCHEMA
    )
    assert "unknown field" in validate_instance({**GOOD_OBJ, "extra": 1}, SCHEMA)
    assert "expected string" in validate_instance(
        {"rationale": 5, "severity": "low", "note": None}, SCHEMA
    )
    nested = {
        "type": "object",
        "additionalProperties": False,
        "required": ["results"],
        "properties": {
            "results": {"type": "array", "items": {"type": "string", "enum": ["a"]}}
        },
    }
    assert validate_instance({"results": ["a"]}, nested) is None
    assert "[1]" in validate_instance({"results": ["a", "b"]}, nested)


# --------------------------------------------------------------------------- #
# Omittable-default coercion (fault-isolation for providers that don't hard-enforce
# strict json_schema — e.g. opus-4-8 via Vertex dropping a finding's clause_types)
# --------------------------------------------------------------------------- #
# Mirrors the finding schema shape: a required string-array (like clause_types), a required
# nullable (guidance), a required enum verdict scalar (change_type — the field opus dropped in
# prod), and other required scalars (which have NO shape-based default).
FINDINGS_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "clause_types",
                    "span",
                    "rationale",
                    "change_type",
                    "severity",
                    "guidance",
                ],
                "properties": {
                    "clause_types": {"type": "array", "items": {"type": "string"}},
                    "span": {"type": "string"},
                    "rationale": {"type": "string"},
                    "change_type": {
                        "type": "string",
                        "enum": ["addition", "deletion", "modification", "absent"],
                    },
                    "severity": {"type": "string", "enum": ["low", "high"]},
                    "guidance": {"type": ["string", "null"]},
                },
            },
        }
    },
}


def test_coerce_fills_omitted_array_and_nullable_but_not_scalar() -> None:
    # A finding missing clause_types (array) and guidance (nullable) — both shape-fillable; the
    # scalar/enum fields (span, change_type) are present so the object conforms after coercion.
    obj = {
        "findings": [
            {
                "span": "s",
                "rationale": "r",
                "change_type": "modification",
                "severity": "low",
            }
        ]
    }
    filled = _coerce_omitted_defaults(obj, FINDINGS_SCHEMA)
    assert obj["findings"][0]["clause_types"] == []
    assert obj["findings"][0]["guidance"] is None
    # Changes are prefixed: '+' = filled a missing field, '-' = stripped an unknown field.
    assert set(filled) == {"+$.findings[0].clause_types", "+$.findings[0].guidance"}
    assert validate_instance(obj, FINDINGS_SCHEMA) is None  # now conforms

    # A required scalar (span) with NO caller default has no shape default — left missing → fails.
    obj2 = {
        "findings": [
            {
                "clause_types": ["x"],
                "rationale": "r",
                "change_type": "modification",
                "severity": "low",
                "guidance": None,
            }
        ]
    }
    filled2 = _coerce_omitted_defaults(obj2, FINDINGS_SCHEMA)
    assert filled2 == []
    assert "span" in validate_instance(obj2, FINDINGS_SCHEMA)


def test_coerce_never_overwrites_present_null() -> None:
    obj = {
        "findings": [
            {
                "clause_types": ["x"],
                "span": "s",
                "rationale": "r",
                "change_type": "modification",
                "severity": "low",
                "guidance": None,  # present-null must survive coercion untouched
            }
        ]
    }
    assert _coerce_omitted_defaults(obj, FINDINGS_SCHEMA) == []
    assert obj["findings"][0]["guidance"] is None


def test_complete_coerces_dropped_array_without_a_repair_round_trip() -> None:
    # opus drops clause_types + guidance; the gateway fills them and returns WITHOUT the repair call.
    dropped = ok_body(
        text=json.dumps(
            {
                "findings": [
                    {
                        "span": "s",
                        "rationale": "r",
                        "change_type": "modification",
                        "severity": "low",
                    }
                ]
            }
        )
    )
    handler, seen = capture([dropped])
    raw = make_adapter(handler).complete(make_req(schema=FINDINGS_SCHEMA))
    assert len(seen) == 1  # NO repair round-trip — coercion resolved it
    assert json.loads(raw.text)["findings"][0]["clause_types"] == []


def test_complete_dropped_scalar_without_caller_default_still_repairs() -> None:
    # A dropped scalar (span) with no caller default is NOT coerced → the repair round-trip runs.
    bad = ok_body(
        text=json.dumps(
            {
                "findings": [
                    {
                        "clause_types": ["x"],
                        "rationale": "r",
                        "change_type": "modification",
                        "severity": "low",
                        "guidance": None,
                    }
                ]
            }
        )
    )  # missing span
    good = ok_body(
        text=json.dumps(
            {
                "findings": [
                    {
                        "clause_types": ["x"],
                        "span": "s",
                        "rationale": "r",
                        "change_type": "modification",
                        "severity": "low",
                        "guidance": None,
                    }
                ]
            }
        )
    )
    handler, seen = capture([bad, good])
    raw = make_adapter(handler).complete(make_req(schema=FINDINGS_SCHEMA))
    assert len(seen) == 2  # dropped scalar forced exactly one repair
    assert json.loads(raw.text)["findings"][0]["span"] == "s"


def test_caller_defaults_retain_a_finding_missing_a_verdict_scalar() -> None:
    # The observed production failure: opus-4-8 via Vertex drops change_type (a required enum). With
    # a recall-safe caller default the finding is RETAINED (not dropped, not a whole-review failure).
    defaults = {"change_type": "modification", "severity": "medium", "rationale": ""}
    # Missing change_type + rationale (caller defaults) + clause_types/guidance (shape); span present.
    obj = {"findings": [{"span": "s", "severity": "high"}]}
    filled = _coerce_omitted_defaults(obj, FINDINGS_SCHEMA, defaults)
    f = obj["findings"][0]
    assert f["change_type"] == "modification"  # caller default wins for the enum
    assert f["rationale"] == ""  # caller default
    assert f["severity"] == "high"  # present value NEVER overwritten by a default
    assert (
        f["clause_types"] == [] and f["guidance"] is None
    )  # shape defaults still apply
    assert "+$.findings[0].change_type" in filled
    assert (
        validate_instance(obj, FINDINGS_SCHEMA) is None
    )  # fully conforms → review survives


def test_complete_caller_default_fills_dropped_enum_without_repair() -> None:
    # End-to-end through complete(): a dropped enum + a caller default → filled, NO repair call.
    dropped = ok_body(
        text=json.dumps(
            {
                "findings": [
                    {
                        "clause_types": ["x"],
                        "span": "s",
                        "rationale": "r",
                        "severity": "low",
                    }
                ]
            }
        )
    )  # missing change_type + guidance
    handler, seen = capture([dropped])
    req = make_req(
        schema=FINDINGS_SCHEMA, coerce_defaults={"change_type": "modification"}
    )
    raw = make_adapter(handler).complete(req)
    assert len(seen) == 1  # coercion resolved it in one shot
    assert json.loads(raw.text)["findings"][0]["change_type"] == "modification"


def test_coerce_strips_hallucinated_unknown_fields() -> None:
    # The observed 2nd-attempt failure: opus adds keys the schema forbids (additionalProperties:
    # false). They're stripped (the engine never reads them) so the batch validates.
    obj = {
        "findings": [
            {
                "clause_types": ["x"],
                "span": "s",
                "rationale": "r",
                "change_type": "modification",
                "severity": "low",
                "guidance": None,
                "clause": "junk",  # hallucinated
                "issue": "junk",  # hallucinated
            }
        ]
    }
    changes = _coerce_omitted_defaults(obj, FINDINGS_SCHEMA)
    assert "clause" not in obj["findings"][0]
    assert "issue" not in obj["findings"][0]
    assert set(changes) == {"-$.findings[0].clause", "-$.findings[0].issue"}
    assert validate_instance(obj, FINDINGS_SCHEMA) is None  # conforms after stripping


def test_complete_strips_unknown_fields_without_a_repair_round_trip() -> None:
    # End-to-end: a reply with only hallucinated extra keys is stripped in one shot, no repair.
    noisy = ok_body(
        text=json.dumps(
            {
                "findings": [
                    {
                        "clause_types": ["x"],
                        "span": "s",
                        "rationale": "r",
                        "change_type": "modification",
                        "severity": "low",
                        "guidance": None,
                        "playbook": "hallucinated",
                    }
                ]
            }
        )
    )
    handler, seen = capture([noisy])
    raw = make_adapter(handler).complete(make_req(schema=FINDINGS_SCHEMA))
    assert len(seen) == 1  # stripped in one shot — NO repair round-trip
    assert "playbook" not in json.loads(raw.text)["findings"][0]


# Gateway-builder / adapter-selection tests (the old `routes_v1.build_engine_gateways`, which
# chose between OpenRouter and a direct-Anthropic fallback per request) were dropped along with
# `routes_v1.py`: OpenRouter is now the ONLY provider, and the key is the signed-in user's own
# (Phase 1), never read from process-wide settings. Phase 2's `agents/orchestrator.py` gets its
# own gateway-construction tests once that replacement exists.
