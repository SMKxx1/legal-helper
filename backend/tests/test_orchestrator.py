"""``app.agents.orchestrator.run_review`` against a FAKE gateway (no network, deterministic JSON
per agent role) — the phase's end-to-end sanity check: the classifier/reviewer/coverage fan-out
produces findings, the merge step (span verification, dedupe, risk tier, adherence score) runs
deterministically, and one ``LlmCallRecord`` lands per fake gateway call.

The span-fabrication test is the SAFETY-CRITICAL one: a reviewer finding whose cited ``span`` is
NOT a verbatim substring of the document must come back ``span_faithful=False`` — the orchestrator
must never trust a hallucinated citation (this is what stops the add-in from ever auto-applying a
tracked change against text that isn't really there).
"""

from __future__ import annotations

import json

import pytest

from app.agents import orchestrator
from app.ai.gateway import Gateway, RawResult, Usage

DOC_TEXT = (
    "MASTER SERVICES AGREEMENT\n\n"
    "1. Confidentiality. Each party shall keep the other's Confidential Information secret.\n\n"
    "12. Limitation of Liability. In no event shall either party's total liability under this "
    "Agreement exceed an unlimited amount, without any cap whatsoever.\n\n"
    "13. Assignment. Neither party may assign this Agreement without the other's consent.\n"
)

_REAL_SPAN = (
    "In no event shall either party's total liability under this "
    "Agreement exceed an unlimited amount, without any cap whatsoever."
)
_FABRICATED_SPAN = "This sentence was never written anywhere in the document."


class FakeAdapter:
    """Answers each agent role with canned, schema-valid JSON. Counts calls (== one per gateway
    ``run()``, since a valid first reply never triggers the repair round-trip)."""

    name = "fake"
    model_id = "fake-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, req) -> RawResult:
        self.calls += 1
        if req.role == "classifier":
            obj = {
                "one_line_summary": "A master services agreement between two companies.",
                "parties": ["Acme Corp", "Widgets Inc"],
                "governing_law": "Delaware",
                "doc_type": "master_services_agreement",
                "our_side_guess": "the Customer",
                "confidence": "high",
            }
        elif req.role == "reviewer":
            obj = {
                "findings": [
                    {
                        "clause_type": "limitation_of_liability",
                        "clause_heading": "12. Limitation of Liability",
                        "span": _REAL_SPAN,
                        "suggested_language": "exceed the fees paid in the prior 12 months",
                        "rationale": "Liability is uncapped, exposing our side to unlimited risk.",
                        "playbook_position": "limitation_of_liability",
                        "severity": "high",
                        "change_type": "modification",
                        "title": "Uncapped liability",
                        "confidence": "high",
                    },
                    {
                        "clause_type": "other",
                        "clause_heading": "",
                        "span": _FABRICATED_SPAN,  # NOT present in DOC_TEXT — hallucinated
                        "suggested_language": "",
                        "rationale": "A fabricated citation the model invented.",
                        "playbook_position": "",
                        "severity": "medium",
                        "change_type": "modification",
                        "title": "Fabricated finding",
                        "confidence": "low",
                    },
                ]
            }
        elif req.role == "coverage":
            obj = {
                "results": [
                    {
                        "clause_type": "confidentiality",
                        "span": "keep the other's Confidential Information secret",
                        "status": "present",
                        "note": None,
                    },
                    {
                        "clause_type": "term_and_termination",
                        "span": None,
                        "status": "absent",
                        "note": "No term or termination clause found.",
                    },
                    {
                        "clause_type": "limitation_of_liability",
                        "span": _REAL_SPAN,
                        "status": "present",
                        "note": None,
                    },
                    {
                        "clause_type": "governing_law_and_disputes",
                        "span": None,
                        "status": "absent",
                        "note": "No governing-law clause found.",
                    },
                ]
            }
        else:
            raise AssertionError(f"unexpected agent role: {req.role!r}")
        return RawResult(
            text=json.dumps(obj),
            usage=Usage(input_tokens=100, output_tokens=50, cost_usd=0.01),
            model_version=self.model_id,
        )


@pytest.fixture(autouse=True)
def _fake_gateways(monkeypatch):
    """Route every gateway the orchestrator builds through a FakeAdapter — no network."""

    def _make_gateway(model, api_key, *, provider_only=()):
        return Gateway(FakeAdapter())

    monkeypatch.setattr(orchestrator, "_make_gateway", _make_gateway)


def test_deep_review_end_to_end_with_fake_gateway():
    result = orchestrator.run_review(DOC_TEXT, "deep", "", "sk-or-fake")

    assert result.doc_type == "master_services_agreement"
    assert result.our_side == "the Customer"  # classifier's guess, since our_side was blank
    assert not result.warnings  # nothing degraded

    # Both findings survive the merge (severity != "none"); only the fabricated one loses
    # span_faithful. Nothing is silently dropped for being unfaithful — it stays advisory.
    assert len(result.findings) == 2
    real, fabricated = (
        next(f for f in result.findings if f.span == _REAL_SPAN or f.span in DOC_TEXT),
        next(f for f in result.findings if f.title == "Fabricated finding"),
    )
    assert real.span_faithful is True
    assert real.span in DOC_TEXT  # snapped to the document's own verbatim text

    # --- THE safety-critical assertion: a hallucinated citation is REJECTED, not trusted. ---
    assert fabricated.span_faithful is False

    # Coverage: two required items absent -> both required for risk tier + adherence.
    assert result.coverage is not None
    assert {"term_and_termination", "governing_law_and_disputes"} == {
        a["clause_type"] for a in result.coverage.absent_required
    }

    # risk_tier: a high finding (and absent-required items) -> red.
    assert result.risk_tier == "red"
    assert result.counts == {"high": 1, "medium": 1, "low": 0}
    assert 0.0 <= result.adherence_score <= 100.0

    # One LlmCallRecord per fake gateway call: classifier + reviewer + coverage (deep mode).
    assert len(result.calls) == 3
    assert {c.agent for c in result.calls} == {"classifier", "reviewer", "coverage"}
    assert all(c.ok for c in result.calls)
    assert result.cost_usd == pytest.approx(0.03)  # 3 calls x $0.01


def test_quick_review_skips_coverage():
    result = orchestrator.run_review(DOC_TEXT, "quick", "the Customer", "sk-or-fake")

    assert result.our_side == "the Customer"  # explicit our_side wins over the classifier's guess
    assert result.coverage is None
    # classifier + reviewer only — no coverage call in quick mode.
    assert len(result.calls) == 2
    assert {c.agent for c in result.calls} == {"classifier", "reviewer"}
    # No absent_required (coverage didn't run) -> risk tier driven by findings alone, still red
    # (the high finding survives quick mode's triage style too).
    assert result.risk_tier == "red"


def test_classifier_failure_is_fail_soft(monkeypatch):
    """A classifier failure must never fail the review — it proceeds with doc_type='unknown' and a
    warning, and the reviewer/coverage calls still run normally."""
    from app.ai.gateway import TerminalProviderError

    class _ClassifierFailsAdapter(FakeAdapter):
        def complete(self, req):
            if req.role == "classifier":
                self.calls += 1
                raise TerminalProviderError("boom")
            return super().complete(req)

    def _make_gateway(model, api_key, *, provider_only=()):
        return Gateway(_ClassifierFailsAdapter())

    monkeypatch.setattr(orchestrator, "_make_gateway", _make_gateway)

    result = orchestrator.run_review(DOC_TEXT, "quick", "", "sk-or-fake")
    assert result.doc_type == "unknown"
    assert any(w.startswith("classifier_failed:") for w in result.warnings)
    assert len(result.findings) == 2  # the reviewer still ran fine


def test_reviewer_failure_is_fail_closed(monkeypatch):
    """A reviewer failure is the sole finding source failing — it must propagate, not degrade."""
    from app.ai.gateway import TerminalProviderError

    class _ReviewerFailsAdapter(FakeAdapter):
        def complete(self, req):
            if req.role == "reviewer":
                raise TerminalProviderError("boom")
            return super().complete(req)

    def _make_gateway(model, api_key, *, provider_only=()):
        return Gateway(_ReviewerFailsAdapter())

    monkeypatch.setattr(orchestrator, "_make_gateway", _make_gateway)

    with pytest.raises(TerminalProviderError):
        orchestrator.run_review(DOC_TEXT, "quick", "", "sk-or-fake")
