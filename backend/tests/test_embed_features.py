"""Wire-up tests for the escalate-only embedding features (S1 pre-check, S2 risk hints, S4 sim).

These use the deterministic "fake" provider and a synthetic in-test index (same shape as
``test_embed_align``), never a real npz or network. They assert the HARD INVARIANT — embeddings may
ADD prompt content / findings / metadata but never remove, shrink, or skip an LLM call, and a
provider that is off produces byte-identical engine requests.
"""

from __future__ import annotations

import json

from app.ai.gateway import Gateway, GatewayRequest, RawResult, Usage
from app.engine import embeddings
from app.engine.embed_align import make_clause_sim, precheck_absent
from app.engine.embeddings import PlaybookIndex
from app.engine.review_service import run_review
from app.engine.simcache import norm_sha256
from app.engine.wholedoc import build_wholedoc_request
from app.playbook.coverage import ChecklistItem

VARIANT = "US_Company"

# Two required clause types. The fake provider is SEMANTICS-FREE (cosine 1.0 only on byte-identical
# text), so — as in test_embed_align — each query text is made identical to the exact clause `.text`
# the segmenter emits (it keeps the "Section N." prefix), giving deterministic attribution in-test.
_TERM_TEXT = "Section 1. This Agreement remains in effect for two (2) years from the Effective Date."
_GOV_TEXT = (
    "Section 2. This Agreement is governed by the laws of the State of Delaware."
)
_TRIGGER = (
    "Perpetual survival with no fixed end date for the confidentiality obligation."
)

_CHECKLIST = [
    ChecklistItem(
        "clause:term_of_confidentiality",
        "term_of_confidentiality",
        "term of confidentiality",
        _TERM_TEXT,
        "clause",
    ),
    ChecklistItem(
        "clause:governing_law_jurisdiction",
        "governing_law_jurisdiction",
        "governing law jurisdiction",
        _GOV_TEXT,
        "clause",
    ),
]


def _fake():
    return embeddings._FakeProvider(model="fake-test")


def _index() -> PlaybookIndex:
    prov = _fake()
    base_texts = [_TERM_TEXT, _GOV_TEXT]
    base_v = prov.embed(base_texts)
    base_m = [{"text": t, "sha": norm_sha256(t)} for t in base_texts]
    trig_v = prov.embed([_TRIGGER])
    trig_m = [{"text": _TRIGGER, "sha": norm_sha256(_TRIGGER)}]
    return PlaybookIndex(
        release="test-release",
        vectors={(VARIANT, "baseline"): base_v, (VARIANT, "triggers"): trig_v},
        meta={(VARIANT, "baseline"): base_m, (VARIANT, "triggers"): trig_m},
    )


# --------------------------------------------------------------------------- #
# S1 — quick-tier deletion pre-check
# --------------------------------------------------------------------------- #
def test_s1_fires_when_a_required_clause_is_missing():
    """A doc containing only the term clause -> governing-law is a candidate-absent."""
    doc = _TERM_TEXT  # governing_law text is NOT present
    cands = precheck_absent(doc, _CHECKLIST, _fake())
    types = {c.clause_type for c in cands}
    assert "governing_law_jurisdiction" in types
    assert "term_of_confidentiality" not in types  # present verbatim -> cosine 1.0


def test_s1_silent_when_all_required_present():
    """A doc that contains BOTH required clauses verbatim -> no candidates."""
    doc = _TERM_TEXT + "\n" + _GOV_TEXT
    assert precheck_absent(doc, _CHECKLIST, _fake()) == []


def test_s1_off_provider_is_silent():
    """Provider None (embeddings off) -> no pre-check candidates, ever."""
    assert precheck_absent(_TERM_TEXT, _CHECKLIST, None) == []


# --------------------------------------------------------------------------- #
# S2 — walk-away-proximity hints in the whole-doc request
# --------------------------------------------------------------------------- #
def test_s2_hints_present_in_task_not_stable_blocks():
    """With hints, the risk-area block is appended to the volatile task and NEVER to stable blocks."""
    hints = ["clause 3 resembles: Perpetual survival with no fixed end date."]
    req = build_wholedoc_request(
        "STANDARD", "DOC BODY", "PLAYBOOK BLOCK", "v-test", risk_hints=hints
    )
    assert "POSSIBLE RISK AREAS" in req.task
    assert hints[0] in req.task
    # The stable prefix (cached) must stay clean of the volatile hints.
    assert not any("POSSIBLE RISK AREAS" in b for b in req.stable_blocks)
    assert "POSSIBLE RISK AREAS" not in req.system


def test_s2_absent_when_no_hits_and_byte_identical_to_control():
    """No hints -> the built request is byte-identical to the pre-embedding request (invariant)."""
    control = build_wholedoc_request("STANDARD", "DOC", "PB", "v-test")
    with_empty = build_wholedoc_request(
        "STANDARD", "DOC", "PB", "v-test", risk_hints=[]
    )
    none_hints = build_wholedoc_request(
        "STANDARD", "DOC", "PB", "v-test", risk_hints=None
    )
    assert with_empty.task == control.task
    assert none_hints.task == control.task
    assert "POSSIBLE RISK AREAS" not in control.task


# --------------------------------------------------------------------------- #
# S4 — clause-similarity refinement (never worse than difflib)
# --------------------------------------------------------------------------- #
def test_s4_sim_is_none_when_provider_off():
    """make_clause_sim(None) -> None so align_clauses keeps its difflib primitive (identical)."""
    assert make_clause_sim(None) is None


def test_s4_cosine_identical_text_is_one():
    sim = make_clause_sim(_fake())
    assert sim is not None
    assert abs(sim(_TERM_TEXT, _TERM_TEXT) - 1.0) < 1e-5


# --------------------------------------------------------------------------- #
# End-to-end: provider off -> identical GatewayRequests; advisory finding shape
# --------------------------------------------------------------------------- #
_DOC = _TERM_TEXT  # single required clause present; governing-law missing
_STANDARD = _TERM_TEXT + "\n" + _GOV_TEXT
_PLAYBOOK = {
    "positions": [
        {
            "clause_type": "term_of_confidentiality",
            "risk_weight": 3,
            "standard_position": _TERM_TEXT,
        },
        {
            "clause_type": "governing_law_jurisdiction",
            "risk_weight": 2,
            "standard_position": _GOV_TEXT,
        },
    ]
}
_ROUTER_OK = {
    "rationale": "Standard mutual NDA.",
    "is_nda": True,
    "perspective": "mutual",
    "our_role": "receiving",
    "paper_owner": "counterparty",
    "jurisdiction": "us",
    "counterparty_type": "company",
    "suggested_variant": "company_mutual",
    "confidence": "high",
}


class _SpyAdapter:
    """ProviderAdapter double that records every GatewayRequest it is handed."""

    name = "fake"
    model_id = "fake-model"

    def __init__(self, responses: dict[str, dict]) -> None:
        self.responses = responses
        self.reqs: list[GatewayRequest] = []

    def complete(self, req):
        self.reqs.append(req)
        obj = self.responses.get(req.role)
        assert obj is not None, f"no canned response for role {req.role!r}"
        return RawResult(
            text=json.dumps(obj),
            usage=Usage(input_tokens=10, output_tokens=5, cost_usd=0.001),
            model_version="fake-model",
        )


def _run(*, clause_match, clause_sim, embed_provider, checklist=_CHECKLIST):
    primary = _SpyAdapter({"wholedoc": {"findings": []}})
    router = _SpyAdapter({"router": _ROUTER_OK})
    result = run_review(
        Gateway(primary),
        incoming_text=_DOC,
        standard_text=_STANDARD,
        playbook=_PLAYBOOK,
        playbook_version="v-test",
        checklist=checklist,
        clause_pass=False,
        whole_doc=True,
        skip_coverage=True,
        self_verify=False,
        wholedoc_style="triage",
        gw_router=Gateway(router),
        clause_match=clause_match,
        clause_sim=clause_sim,
        embed_provider=embed_provider,
    )
    return result, primary


def _req_fields(reqs: list[GatewayRequest]) -> list[tuple]:
    return [(r.role, r.system, tuple(r.stable_blocks), r.task) for r in reqs]


def test_provider_off_builds_identical_requests_to_control():
    """The HARD INVARIANT: embeddings off -> the GatewayRequests are IDENTICAL to a control run."""
    _control, control_primary = _run(
        clause_match=None, clause_sim=None, embed_provider=None
    )
    # A second run with everything still off must build byte-identical requests.
    _again, again_primary = _run(
        clause_match=None, clause_sim=None, embed_provider=None
    )
    assert _req_fields(control_primary.reqs) == _req_fields(again_primary.reqs)


def test_s1_advisory_finding_shape_and_off_by_default():
    """With the fake provider + a missing required clause, S1 appends ONE advisory finding whose
    span is empty and span_faithful is falsy (no Apply affordance); and it never runs when off."""
    on, _ = _run(clause_match=None, clause_sim=None, embed_provider=_fake())
    precheck = on.embed_precheck
    assert any(c["clause_type"] == "governing_law_jurisdiction" for c in precheck)
    advisory = [f for f in on.findings if f.get("source") == "embed_precheck"]
    assert advisory, "expected an S1 advisory finding"
    f = advisory[0]
    assert f["span"] == ""
    assert not f["span_faithful"]  # falsy -> flows through span repair with no Apply
    assert f["severity"] == "medium"
    assert f["change_type"] == "absent"
    assert f["title"].startswith("Required clause possibly absent:")

    # Off -> no advisory finding and an empty embed_precheck list.
    off, _ = _run(clause_match=None, clause_sim=None, embed_provider=None)
    assert off.embed_precheck == []
    assert not [f for f in off.findings if f.get("source") == "embed_precheck"]


def test_s1_absent_in_deep_mode():
    """Deep runs coverage (skip_coverage=False) -> the S1 pre-check must NOT fire even with a
    provider, so it never double-counts against deep's authoritative coverage net."""
    primary = _SpyAdapter({"wholedoc": {"findings": []}, "coverage": {"results": []}})
    router = _SpyAdapter({"router": _ROUTER_OK})
    result = run_review(
        Gateway(primary),
        incoming_text=_DOC,
        standard_text=_STANDARD,
        playbook=_PLAYBOOK,
        playbook_version="v-test",
        checklist=_CHECKLIST,
        clause_pass=False,
        whole_doc=True,
        skip_coverage=False,  # deep
        self_verify=False,
        wholedoc_style="edit",
        gw_router=Gateway(router),
        embed_provider=_fake(),
    )
    assert result.embed_precheck == []
    assert not [f for f in result.findings if f.get("source") == "embed_precheck"]
