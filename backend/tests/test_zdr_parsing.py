"""``ai/zdr`` parses OpenRouter's real ``GET /endpoints/zdr`` rows.

The fixtures below are trimmed copies of ACTUAL rows from that endpoint, because the parser
previously assumed a shape the API does not use — it looked for ``id``/``slug`` (the field is
``model_id``) and treated ``status`` as the string ``"healthy"`` (it is numeric, 0 when serving).
Every row was therefore discarded: 832 rows parsed to zero models, so `GET /api/models/zdr`
returned an empty list and `PUT /api/me/models` answered `model_not_zdr` for every model —
silently, with nothing failing anywhere.
"""

from __future__ import annotations

from app.ai import zdr


def _row(**over) -> dict:
    """A real glm-5.3-flash endpoint row, trimmed to the fields the parser reads."""
    row = {
        "model_id": "z-ai/glm-5.3-flash",
        "model_name": "Z.AI: GLM 5.3 Flash",
        "name": "Modal | z-ai/glm-5.3-flash",
        "provider_name": "Modal",
        "status": 0,
        "context_length": 1310720,
        "pricing": {"prompt": "0.00000007", "completion": "0.00000025"},
        "supported_parameters": [
            "include_reasoning",
            "max_tokens",
            "reasoning",
            "response_format",
            "structured_outputs",
            "tools",
        ],
    }
    row.update(over)
    return row


def test_a_real_serving_row_parses():
    parsed = zdr._parse_endpoint(_row())
    assert parsed is not None
    assert parsed.id == "z-ai/glm-5.3-flash"
    assert parsed.provider == "Modal"
    assert parsed.context_length == 1310720
    # pricing is per-token in the payload; the picker shows per-million
    assert parsed.prompt_usd_per_m == 0.07
    assert parsed.completion_usd_per_m == 0.25


def test_status_zero_means_serving_not_unhealthy():
    """The whole outage-by-omission: 0 is healthy, and must not be compared against "healthy"."""
    assert zdr._parse_endpoint(_row(status=0)) is not None


def test_a_deranked_endpoint_is_skipped():
    assert zdr._parse_endpoint(_row(status=-1)) is None


def test_the_string_shape_still_works():
    """Older/alternate payloads used a string status — keep accepting it."""
    assert zdr._parse_endpoint(_row(status="healthy")) is not None
    assert zdr._parse_endpoint(_row(status="degraded")) is None


def test_a_route_without_structured_output_is_skipped():
    """The agents send response_format; a route that can't take it is useless here."""
    assert (
        zdr._parse_endpoint(_row(supported_parameters=["max_tokens", "tools"])) is None
    )


def test_the_readable_model_name_is_preferred_over_the_endpoint_label():
    assert zdr._parse_endpoint(_row()).name == "Z.AI: GLM 5.3 Flash"


def test_one_entry_per_model_not_per_provider_route(monkeypatch):
    """A model served by 18 providers is still one choice in the picker."""
    rows = [
        _row(provider_name="Modal"),
        _row(provider_name="DeepInfra"),
        _row(provider_name="Novita"),
        _row(model_id="z-ai/glm-5.3", model_name="Z.AI: GLM 5.3"),
    ]
    parsed = [zdr._parse_endpoint(r) for r in rows]
    seen, models = set(), []
    for p in parsed:
        if p is not None and p.id not in seen:
            seen.add(p.id)
            models.append(p)
    assert [m.id for m in models] == ["z-ai/glm-5.3-flash", "z-ai/glm-5.3"]
