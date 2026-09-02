"""Portable JSON schemas for the three agents (plan §4.2). Every schema obeys
``app.engine.portable_schema``'s strict-mode intersection (validated at import, below) and the D1
left-to-right decoding rule: reasoning/evidence fields precede the verdict fields.
"""

from __future__ import annotations

from app.engine.portable_schema import assert_portable, assert_reasoning_before_verdict

# --------------------------------------------------------------------------- #
# Classifier — doc_type, parties, governing_law, our_side_guess, one_line_summary, confidence
# --------------------------------------------------------------------------- #
CLASSIFIER_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "one_line_summary",
        "doc_type",
        "parties",
        "governing_law",
        "our_side_guess",
        "confidence",
    ],
    "properties": {
        "one_line_summary": {"type": "string"},  # reasoning first (D1)
        "parties": {"type": "array", "items": {"type": "string"}},
        "governing_law": {"type": ["string", "null"]},
        "doc_type": {"type": "string"},  # e.g. "nda", "msa", "employment_agreement" — verdict
        "our_side_guess": {"type": "string"},  # e.g. "the Customer", "the Receiving Party"
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
}

# --------------------------------------------------------------------------- #
# Reviewer — one finding per deviation from the playbook that leaves our side worse off.
# Evidence (clause_heading, span, suggested_language, rationale, playbook_position) decodes
# before the verdict (severity, change_type) — D1.
# --------------------------------------------------------------------------- #
FINDING_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "clause_type",
        "clause_heading",
        "span",
        "suggested_language",
        "rationale",
        "playbook_position",
        "severity",
        "change_type",
        "title",
        "confidence",
    ],
    "properties": {
        "clause_type": {"type": "string"},  # a playbook clause_type, or "other"
        "clause_heading": {"type": "string"},  # the document's own heading/label, or ""
        "span": {"type": "string"},  # verbatim quote from the document
        "suggested_language": {"type": "string"},  # replacement text only, or "" to delete
        "rationale": {"type": "string"},
        "playbook_position": {"type": "string"},  # which standard position this deviates from
        "severity": {"type": "string", "enum": ["high", "medium", "low", "none"]},
        "change_type": {
            "type": "string",
            "enum": ["addition", "deletion", "modification", "absent"],
        },
        "title": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
}

REVIEWER_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {"findings": {"type": "array", "items": FINDING_SCHEMA}},
}

#: Recall-safe defaults for a REQUIRED finding field a lenient provider drops (some ZDR routes —
#: e.g. opus-4-8 via Vertex — don't hard-enforce strict json_schema). A partial finding stays
#: visible for manual review instead of failing the whole review.
FINDING_COERCE_DEFAULTS: dict = {
    "clause_type": "other",
    "clause_heading": "",
    "span": "",
    "suggested_language": "",
    "rationale": "",
    "playbook_position": "",
    "severity": "medium",
    "change_type": "modification",
    "title": "Review finding",
    "confidence": "low",
}

# --------------------------------------------------------------------------- #
# Coverage — a CLOSED checklist (derived in code from the playbook's presence:"required"
# positions); the model only answers present/absent + a verbatim span per item.
# --------------------------------------------------------------------------- #
COVERAGE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["clause_type", "span", "status", "note"],
                "properties": {
                    "clause_type": {"type": "string"},
                    "span": {"type": ["string", "null"]},  # evidence first (D1)
                    "status": {"type": "string", "enum": ["present", "absent"]},
                    "note": {"type": ["string", "null"]},
                },
            },
        },
    },
}

for _schema in (CLASSIFIER_SCHEMA, REVIEWER_SCHEMA, FINDING_SCHEMA, COVERAGE_SCHEMA):
    assert assert_portable(_schema)

assert assert_reasoning_before_verdict(
    CLASSIFIER_SCHEMA, ["one_line_summary", "parties"], ["doc_type", "confidence"]
)
assert assert_reasoning_before_verdict(
    FINDING_SCHEMA,
    ["clause_heading", "span", "suggested_language", "rationale", "playbook_position"],
    ["severity", "change_type", "confidence"],
)
