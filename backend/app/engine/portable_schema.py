"""Structured-output schemas + the portability invariants.

A schema is *portable* when it is valid as **strict** structured output on
Anthropic (`output_config.format` json_schema). These rules are the conservative
strict-mode intersection (kept provider-neutral so the schemas stay portable). The
hard constraints:

- every property listed in ``required`` (strict mode requires it);
- optionals expressed as nullable unions, e.g. ``{"type": ["string", "null"]}``;
- ``additionalProperties: false`` on every object;
- ``enum`` IS portable — keep it;
- NO ``minimum``/``maximum``/``minLength``/``maxLength``/``pattern``/``format``/…
  numeric/length constraints — validate those in Python after parse;
- shallow, non-recursive.

Plus the D1 decoding rule: grammar-constrained decoding is left-to-right, so a
schema must emit reasoning/evidence fields BEFORE the verdict field, or the
rationale becomes a post-hoc justification of an already-emitted verdict.
``assert_reasoning_before_verdict`` enforces the field order.
"""

from __future__ import annotations

#: Keywords that are not portable across both providers' strict modes. Validate
#: any of these constraints in Python instead of declaring them in the schema.
BANNED_KEYS: frozenset[str] = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    }
)


def assert_portable(schema: dict, path: str = "$") -> bool:
    """Raise ``ValueError`` unless ``schema`` obeys the intersection rules.

    Returns ``True`` so it can be used in an ``assert`` at import time.
    """
    if not isinstance(schema, dict):
        raise ValueError(
            f"{path}: schema node must be a dict, got {type(schema).__name__}"
        )

    banned = BANNED_KEYS & schema.keys()
    if banned:
        raise ValueError(
            f"{path}: non-portable keyword(s) {sorted(banned)} — validate these in "
            "code, not in the schema"
        )

    t = schema.get("type")
    is_object = (
        t == "object"
        or (isinstance(t, list) and "object" in t)
        or "properties" in schema
    )
    if is_object:
        props = schema.get("properties")
        if not isinstance(props, dict) or not props:
            raise ValueError(f"{path}: object must declare a non-empty 'properties'")
        if schema.get("additionalProperties") is not False:
            raise ValueError(f"{path}: object must set additionalProperties: false")
        required = schema.get("required")
        if not isinstance(required, list) or set(required) != set(props):
            missing = sorted(set(props) - set(required or []))
            extra = sorted(set(required or []) - set(props))
            raise ValueError(
                f"{path}: 'required' must list exactly every property (strict mode); "
                f"missing {missing}, unknown {extra}"
            )
        for key, sub in props.items():
            assert_portable(sub, f"{path}.{key}")

    if "items" in schema:
        assert_portable(schema["items"], f"{path}[]")
    for i, branch in enumerate(schema.get("anyOf", []) or []):
        assert_portable(branch, f"{path}|{i}")
    return True


def assert_reasoning_before_verdict(
    schema: dict, reasoning_fields: list[str], verdict_fields: list[str]
) -> bool:
    """Raise unless every reasoning field precedes every verdict field (D1).

    JSON-Schema ``properties`` order is the generation order under grammar-
    constrained decoding; evidence/rationale must be decoded before the verdict.
    """
    order = list(schema.get("properties", {}))
    idx = {k: i for i, k in enumerate(order)}
    if not reasoning_fields or not verdict_fields:
        raise ValueError("reasoning_fields and verdict_fields must both be non-empty")
    missing = [k for k in (*reasoning_fields, *verdict_fields) if k not in idx]
    if missing:
        raise ValueError(f"fields not present in schema: {missing}")
    last_reasoning = max(idx[k] for k in reasoning_fields)
    first_verdict = min(idx[k] for k in verdict_fields)
    if last_reasoning >= first_verdict:
        raise ValueError(
            "verdict must be decoded after all reasoning fields (left-to-right): "
            f"reasoning ends at index {last_reasoning}, verdict starts at {first_verdict}"
        )
    return True


# --------------------------------------------------------------------------- #
# Canonical per-role schemas (versioned — P2-1). Field order obeys D1.
# --------------------------------------------------------------------------- #

#: T2 per-clause finding. Evidence (clause_types, span, suggested_language,
#: rationale, playbook_position) decodes before the verdict (severity,
#: change_type) — D1 left-to-right decoding.
FINDING_SCHEMA_V1: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "clause_types",
        "span",
        "suggested_language",
        "rationale",
        "playbook_position",
        "severity",
        "change_type",
        "title",
        "confidence",
        "guidance",
    ],
    "properties": {
        "clause_types": {
            "type": "array",
            "items": {"type": "string"},
        },  # multi-label (P2-2)
        "span": {"type": "string"},  # verbatim quote (B)
        "suggested_language": {"type": "string"},  # evidence (D1)
        "rationale": {"type": "string"},
        "playbook_position": {"type": "string"},
        "severity": {
            "type": "string",
            "enum": ["high", "medium", "low", "none"],
        },  # none = cosmetic/non-issue
        "change_type": {
            "type": "string",
            "enum": ["addition", "deletion", "modification", "absent"],
        },
        "title": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "guidance": {"type": ["string", "null"]},
    },
}

#: Recall-safe defaults for REQUIRED finding fields a lenient provider may DROP (opus-4-8 via
#: Vertex doesn't hard-enforce strict json_schema). Passed as ``GatewayRequest.coerce_defaults`` so
#: a partial finding is RETAINED + VISIBLE instead of failing the whole review — a dropped field
#: must never silently hide a finding (mirrors ``findings._fallback_finding``: severity=medium,
#: never GREEN). Array/nullable fields (clause_types, guidance) are filled by the gateway's
#: shape-based default and need no entry here.
FINDING_COERCE_DEFAULTS: dict = {
    "span": "",
    "suggested_language": "",
    "rationale": "",
    "playbook_position": "",
    "severity": "medium",  # recall-safe: visible for manual review, never silently dropped
    "change_type": "modification",  # neutral; the finding still surfaces for review
    "title": "Review finding",
    "confidence": "low",
}

#: T1.6 coverage result — the model fills present/absent + span for each closed
#: checklist item it is handed (the checklist itself is derived in code: A).
COVERAGE_RESULT_SCHEMA_V1: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["item_key", "span", "status", "note"],
                "properties": {
                    "item_key": {"type": "string"},
                    "span": {"type": ["string", "null"]},  # evidence first (D1)
                    "status": {"type": "string", "enum": ["present", "absent"]},
                    "note": {"type": ["string", "null"]},
                },
            },
        },
    },
}

#: T0 router. Rationale decodes before the routing decision (D1).
ROUTER_SCHEMA_V1: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "rationale",
        "is_nda",
        "perspective",
        "our_role",
        "paper_owner",
        "jurisdiction",
        "counterparty_type",
        "suggested_variant",
        "confidence",
    ],
    "properties": {
        "rationale": {"type": "string"},
        "is_nda": {"type": "boolean"},
        "perspective": {"type": "string", "enum": ["one_way", "mutual", "unknown"]},
        "our_role": {
            "type": "string",
            "enum": ["disclosing", "receiving", "both", "unknown"],
        },
        "paper_owner": {
            "type": "string",
            "enum": ["amperesand", "counterparty", "third_party", "unknown"],
        },
        "jurisdiction": {"type": "string", "enum": ["us", "sg", "unknown"]},
        "counterparty_type": {
            "type": "string",
            "enum": ["company", "individual", "service_provider", "unknown"],
        },
        "suggested_variant": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
}

#: Clause-change severity rating (used by the ensemble verify + calibration).
#: Rationale decodes before the verdict (severity, confidence) — D1.
RATE_SCHEMA_V1: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["rationale", "severity", "confidence"],
    "properties": {
        "rationale": {"type": "string"},
        "severity": {"type": "string", "enum": ["none", "low", "medium", "high"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
}

# Fail fast at import if a canonical schema drifts out of portability / ordering.
assert assert_portable(FINDING_SCHEMA_V1)
assert assert_portable(COVERAGE_RESULT_SCHEMA_V1)
assert assert_portable(ROUTER_SCHEMA_V1)
assert assert_portable(RATE_SCHEMA_V1)
assert assert_reasoning_before_verdict(
    RATE_SCHEMA_V1, ["rationale"], ["severity", "confidence"]
)

#: T3 cross-clause interaction flags. Within each flag: clauses + issue (reasoning)
#: before severity (verdict) — D1.
CROSS_CLAUSE_SCHEMA_V1: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["flags"],
    "properties": {
        "flags": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["clauses", "issue", "severity"],
                "properties": {
                    "clauses": {"type": "array", "items": {"type": "string"}},
                    "issue": {"type": "string"},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
        },
    },
}
assert assert_portable(CROSS_CLAUSE_SCHEMA_V1)
assert assert_reasoning_before_verdict(
    FINDING_SCHEMA_V1,
    ["clause_types", "span", "suggested_language", "rationale", "playbook_position"],
    ["severity", "change_type", "confidence"],
)
assert assert_reasoning_before_verdict(
    ROUTER_SCHEMA_V1, ["rationale"], ["is_nda", "perspective", "confidence"]
)
