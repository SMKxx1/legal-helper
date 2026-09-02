"""Engine happy-path characterization via a provider test double.

The live review pipeline (router -> whole-doc structured review -> deterministic synthesis) had NO
end-to-end test because every pass hits a real provider. ``FakeAdapter`` implements the
``ProviderAdapter`` contract and returns canned, schema-valid JSON per ``req.role``, so ``run_review``
runs fully offline. These tests lock the tier / adherence / findings contract the API serializes —
the previously-untested core, and the path the false-GREEN fix guards.
"""

from __future__ import annotations

import io
import json

from app.ai.gateway import Gateway, RawResult, Usage
from app.engine.review_service import run_review

_DOC = (
    "Section 1. Confidentiality. The Receiving Party shall keep the Confidential Information "
    "secret and shall not disclose it to any third party without prior written consent.\n"
    "Section 2. Term. This Agreement remains in effect for three (3) years."
)
_STANDARD = (
    "Section 1. Confidentiality. The Receiving Party shall keep the Confidential Information "
    "secret.\nSection 2. Term. This Agreement remains in effect for two (2) years."
)
_PLAYBOOK = {
    "positions": [
        {
            "clause_type": "term_of_confidentiality",
            "risk_weight": 3,
            "standard_position": "Confidentiality survives for two (2) years.",
        }
    ]
}

_ROUTER_OK = {
    "rationale": "Standard mutual NDA on counterparty paper.",
    "is_nda": True,
    "perspective": "mutual",
    "our_role": "receiving",
    "paper_owner": "counterparty",
    "jurisdiction": "us",
    "counterparty_type": "company",
    "suggested_variant": "company_mutual",
    "confidence": "high",
}


def _finding(severity: str) -> dict:
    return {
        "clause_types": ["term_of_confidentiality"],
        "span": "three (3) years",  # verbatim in _DOC, so repair_span snaps it
        "suggested_language": "two (2) years",
        "rationale": "Confidentiality term extended beyond the standard two years.",
        "playbook_position": "Confidentiality survives for two (2) years.",
        "severity": severity,
        "change_type": "modification",
        "title": "Confidentiality term lengthened",
        "confidence": "high",
        "guidance": "Restore the 2-year term.",
    }


class FakeAdapter:
    """ProviderAdapter test double: returns canned, schema-valid JSON keyed by ``req.role``."""

    name = "fake"
    model_id = "fake-model"

    def __init__(self, responses: dict[str, dict]) -> None:
        self.responses = responses
        self.roles_seen: list[str] = []

    def complete(self, req):
        self.roles_seen.append(req.role)
        obj = self.responses.get(req.role)
        assert obj is not None, (
            f"FakeAdapter has no canned response for role {req.role!r}"
        )
        return RawResult(
            text=json.dumps(obj),
            usage=Usage(input_tokens=10, output_tokens=5, cost_usd=0.001),
            model_version="fake-model",
        )


class FailingRoleAdapter:
    """ProviderAdapter double that RAISES a retryable outage for selected roles (to exercise the
    gateway fallback / degradation path) and returns canned JSON for the rest."""

    name = "fake"
    model_id = "fake-model"

    def __init__(self, responses: dict[str, dict], *, fail_roles: set[str]) -> None:
        self.responses = responses
        self.fail_roles = set(fail_roles)
        self.roles_seen: list[str] = []

    def complete(self, req):
        from app.ai.gateway import RetryableProviderError

        self.roles_seen.append(req.role)
        if req.role in self.fail_roles:
            raise RetryableProviderError("simulated provider outage")
        obj = self.responses.get(req.role)
        assert obj is not None, (
            f"FailingRoleAdapter has no canned response for role {req.role!r}"
        )
        return RawResult(
            text=json.dumps(obj),
            usage=Usage(input_tokens=10, output_tokens=5, cost_usd=0.001),
            model_version="fake-model",
        )


def _run_quick(wholedoc_obj: dict):
    """Run the production QUICK config (clause_pass=False, whole-doc only) with fake providers."""
    primary = FakeAdapter({"wholedoc": wholedoc_obj})
    router = FakeAdapter({"router": _ROUTER_OK})
    result = run_review(
        Gateway(primary),
        incoming_text=_DOC,
        standard_text=_STANDARD,
        playbook=_PLAYBOOK,
        playbook_version="v-test",
        clause_pass=False,
        whole_doc=True,
        skip_coverage=True,
        self_verify=False,
        wholedoc_style="triage",
        gw_router=Gateway(router),
        mode_label="quick",
    )
    return result, primary, router


def test_quick_review_clean_doc_is_green():
    result, primary, router = _run_quick({"findings": []})
    assert result.risk_tier == "green"
    assert result.findings == []
    assert result.adherence_score == 100.0
    # The fake providers were actually exercised (no real network call).
    assert primary.roles_seen == ["wholedoc"]
    assert router.roles_seen == ["router"]


def test_quick_review_high_finding_is_red():
    result, primary, _ = _run_quick({"findings": [_finding("high")]})
    assert result.risk_tier == "red"
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f["severity"] == "high"
    assert f["source"] == "wholedoc"
    assert result.adherence_score < 100.0


# --------------------------------------------------------------------------- #
# Additive response fields: degradation visibility (#8), quick-mode confidence
# (#2), and provenance (#6).
# --------------------------------------------------------------------------- #
def test_quick_serialize_is_triage_and_reports_no_coverage():
    from app.api.routes_v1 import _serialize

    result, _, _ = _run_quick({"findings": []})
    out = _serialize("r" * 32, result)
    assert out["mode"] == "quick"
    ai = out["analysis_integrity"]
    assert ai["mode"] == "quick"
    assert ai["confidence"] == "triage"
    assert ai["coverage_ran"] is False
    assert ai["degraded_components"] == []
    # coverage block gains ran/degraded (both False for a clean quick pass, which skips coverage).
    assert out["coverage"]["ran"] is False
    assert out["coverage"]["degraded"] is False


def test_coverage_gateway_fallback_marks_degraded():
    from app.api.routes_v1 import _serialize

    # A deep-shaped run (coverage enabled) where the coverage gateway call fails and falls back.
    primary = FailingRoleAdapter(
        {"wholedoc": {"findings": []}}, fail_roles={"coverage"}
    )
    result = run_review(
        Gateway(primary),
        incoming_text=_DOC,
        standard_text=_STANDARD,
        playbook=_PLAYBOOK,
        playbook_version="v-test",
        clause_pass=False,
        whole_doc=True,
        skip_coverage=False,
        self_verify=False,
        wholedoc_style="edit",
        mode_label="deep",
    )
    assert result.coverage.degraded is True
    assert "coverage" in result.degraded_components
    assert result.coverage_ran is True

    out = _serialize("r" * 32, result)
    assert out["coverage"]["ran"] is True
    assert out["coverage"]["degraded"] is True
    assert "coverage" in out["analysis_integrity"]["degraded_components"]
    assert out["analysis_integrity"]["confidence"] == "full"  # deep


def test_router_gateway_fallback_marks_degraded():
    primary = FakeAdapter({"wholedoc": {"findings": []}})
    router = FailingRoleAdapter({}, fail_roles={"router"})
    result = run_review(
        Gateway(primary),
        incoming_text=_DOC,
        standard_text=_STANDARD,
        playbook=_PLAYBOOK,
        playbook_version="v-test",
        clause_pass=False,
        whole_doc=True,
        skip_coverage=True,
        self_verify=False,
        wholedoc_style="triage",
        gw_router=Gateway(router),
        mode_label="quick",
    )
    assert "router" in result.degraded_components


def test_router_degraded_kwarg_is_recorded():
    # A caller (routes_v1) that pre-ran the router passes its fallback flag through even though
    # run_review makes no router call itself (router_obj supplied).
    result, _, _ = _run_quick_with_router_degraded()
    assert "router" in result.degraded_components


def _run_quick_with_router_degraded():
    primary = FakeAdapter({"wholedoc": {"findings": []}})
    result = run_review(
        Gateway(primary),
        incoming_text=_DOC,
        standard_text=_STANDARD,
        playbook=_PLAYBOOK,
        playbook_version="v-test",
        clause_pass=False,
        whole_doc=True,
        skip_coverage=True,
        self_verify=False,
        wholedoc_style="triage",
        router_obj=_ROUTER_OK,
        router_degraded=True,
        mode_label="quick",
    )
    return result, primary, None


def test_provenance_block_is_populated():
    import re

    from app.api.routes_v1 import _serialize

    result, _, _ = _run_quick({"findings": []})
    prov = result.provenance
    assert prov["models"]["primary"] == "fake-model"
    assert prov["models"]["router"] == "fake-model"
    assert prov["provider"] == "fake"
    assert prov["mode"] == "quick"
    assert re.fullmatch(r"[0-9a-f]{16}", prov["prompt_release"])
    assert isinstance(prov["playbook_release"], str)

    out = _serialize("r" * 32, result)
    assert out["provenance"]["prompt_release"] == prov["prompt_release"]
    assert out["provenance"]["models"]["primary"] == "fake-model"


# --------------------------------------------------------------------------- #
# HTTP contract: POST /v1/reviews end-to-end through the real route, with the
# provider double injected (no network). The test env leaves the engine
# unconfigured, so engine_principal binds the open svc:local dev principal.
# --------------------------------------------------------------------------- #
def _fake_gateways(wholedoc_obj: dict) -> dict:
    return {
        "primary": Gateway(FakeAdapter({"wholedoc": wholedoc_obj})),
        "router": Gateway(FakeAdapter({"router": _ROUTER_OK})),
    }


def _inject_fakes(monkeypatch, session_factory, wholedoc_obj: dict) -> None:
    """Pin playbook/standard to existing files (skip v4 variant resolution -> deterministic), swap
    build_engine_gateways for the provider double, and point reviews_repo at the throwaway test DB
    (the route persists via its own SessionLocal, NOT the request's get_db override), so the route
    runs with no network call and no real-DB writes."""
    from app.api import reviews_repo, routes_v1
    from app.config import settings

    monkeypatch.setattr(reviews_repo, "SessionLocal", session_factory)
    monkeypatch.setattr(
        settings,
        "engine_playbook_path",
        str(routes_v1._DEFAULT_PLAYBOOK),
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "engine_standard_template_path",
        str(routes_v1._DEFAULT_STANDARD),
        raising=False,
    )
    monkeypatch.setattr(
        routes_v1,
        "build_engine_gateways",
        lambda cfg, *, mode: _fake_gateways(wholedoc_obj),
    )


def _post_review(client):
    return client.post(
        "/v1/reviews",
        data={"mode": "quick"},
        files={"file": ("nda.txt", io.BytesIO(_DOC.encode()), "text/plain")},
    )


def test_create_review_http_returns_201_and_full_contract(
    client, session_factory, monkeypatch
):
    _inject_fakes(monkeypatch, session_factory, {"findings": [_finding("high")]})

    resp = _post_review(client)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["risk_tier"] == "red"
    assert {
        "review_id",
        "risk_tier",
        "adherence_score",
        "perspective",
        "findings",
        "coverage",
        "redline_plan",
        "counts",
        "cost_usd",
    } <= set(body)
    assert len(body["findings"]) == 1
    assert body["findings"][0]["severity"] == "high"


def test_create_review_exact_resubmit_is_cache_hit(
    client, session_factory, monkeypatch
):
    _inject_fakes(monkeypatch, session_factory, {"findings": [_finding("high")]})

    first = _post_review(client)
    assert first.status_code == 201

    # The identical document resubmitted is served from the exact-sha cache (200, not a re-run 201).
    second = _post_review(client)
    assert second.status_code == 200
    assert second.json()["review_id"] == first.json()["review_id"]


def test_create_review_exact_resubmit_misses_after_playbook_release_change(
    client, session_factory, monkeypatch
):
    """A playbook change bumps the release id, so the exact-sha cache MISSES the review graded by
    the old release — a fresh review (201) instead of a stale re-serve (audit #3)."""
    from app.api import reviews_repo

    _inject_fakes(monkeypatch, session_factory, {"findings": [_finding("high")]})

    first = _post_review(client)
    assert first.status_code == 201

    # Simulate a new playbook release: the current-release id no longer matches the stored row's.
    monkeypatch.setattr(reviews_repo, "playbook_release_id", lambda: "deadbeefdeadbeef")
    second = _post_review(client)
    assert second.status_code == 201, second.text
    assert second.json()["review_id"] != first.json()["review_id"]


def test_create_review_monthly_cost_cap_returns_429(
    client, session_factory, monkeypatch
):
    from app.config import settings

    _inject_fakes(monkeypatch, session_factory, {"findings": []})
    # A tiny monthly cap: the first review's recorded spend then trips it for the next document.
    monkeypatch.setattr(settings, "engine_monthly_cost_cap_usd", 0.0005, raising=False)

    first = _post_review(client)
    assert (
        first.status_code == 201
    )  # prior spend was 0, so this one runs and records cost

    # A DISTINCT document (cache miss) now exceeds the monthly cap -> 429 BEFORE the engine runs.
    second = client.post(
        "/v1/reviews",
        data={"mode": "quick"},
        files={
            "file": (
                "nda2.txt",
                io.BytesIO((_DOC + " A distinct trailing clause.").encode()),
                "text/plain",
            )
        },
    )
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "cost_cap_exceeded"


def test_redline_returns_stored_plan(client, session_factory, monkeypatch):
    _inject_fakes(monkeypatch, session_factory, {"findings": [_finding("high")]})

    created = _post_review(client)
    assert created.status_code == 201
    review_id = created.json()["review_id"]

    # POST /v1/redline returns the stored review's redline plan (svc:local holds the redline entitlement).
    resp = client.post("/v1/redline", data={"review_id": review_id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["review_id"] == review_id
    assert isinstance(body["redline_plan"], list)


def test_redline_unknown_review_is_404(client, session_factory, monkeypatch):
    _inject_fakes(monkeypatch, session_factory, {"findings": []})
    resp = client.post("/v1/redline", data={"review_id": "0" * 32})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def _docx_bytes(text: str) -> bytes:
    """A real .docx carrying ``text`` (one paragraph per line) — the format real uploads use."""
    from docx import Document

    buf = io.BytesIO()
    doc = Document()
    for line in text.split("\n"):
        doc.add_paragraph(line)
    doc.save(buf)
    return buf.getvalue()


def test_create_review_parses_a_real_docx_end_to_end(
    client, session_factory, monkeypatch
):
    """The CI blind spot the panel flagged: every other HTTP review test submits .txt, so the real
    docx parse path (parse_document -> extract_docx) that actual uploads take is never exercised at the
    route level. Submit an ACTUAL .docx (same clause text as _DOC) and assert the engine still produces
    the high finding — proving extraction fed the review, not an empty/garbled document."""
    _inject_fakes(monkeypatch, session_factory, {"findings": [_finding("high")]})

    resp = client.post(
        "/v1/reviews",
        data={"mode": "quick"},
        files={
            "file": (
                "nda.docx",
                io.BytesIO(_docx_bytes(_DOC)),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["risk_tier"] == "red"
    assert len(body["findings"]) == 1
    assert body["findings"][0]["severity"] == "high"
    # The finding's span ("three (3) years") is verbatim only in the docx body, so its presence proves
    # extract_docx recovered the clause text and the engine reviewed it.
    assert body["findings"][0]["span"] == "three (3) years"
