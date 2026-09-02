"""The review pipeline's orchestrator (plan §4.2, adapted from the old ``engine/review_service.py``)
— "basic agent orchestration", the workshop's teaching point for this module:

    classifier  (fast, first slice of the document)
        |
        v  (fan out — ThreadPoolExecutor, ctx_copy so the usage ledger follows the threads)
    reviewer  ∥  coverage (deep only)
        |
        v
    deterministic merge (no LLM): span verification, dedupe, risk tier, adherence score

Fail-soft vs fail-closed, on purpose (plan §4.2):

* classifier failure -> proceed with ``doc_type="unknown"`` (nothing downstream depends on it).
* coverage failure    -> ``coverage=None`` + a warning (deep mode still has the reviewer's findings).
* reviewer failure    -> FAIL CLOSED: this is the sole finding source, so the exception propagates
  (a :class:`~app.ai.gateway.ProviderError`) and the caller maps it to a review error code via
  :func:`~app.ai.gateway.error_code_for`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from app.agents.classifier import (
    UNKNOWN_CLASSIFICATION,
    ClassifierResult,
    run_classifier,
)
from app.agents.coverage import CoverageItemResult, run_coverage
from app.agents.reviewer import merge_findings, run_reviewer
from app.ai.gateway import Gateway, ProviderError, error_code_for
from app.ai.ledger import LlmCallRecord, track_usage
from app.ai.ledger import ctx_copy as _ctx_copy
from app.ai.openrouter import OpenRouterAdapter
from app.config import settings
from app.engine.spans import repair_span
from app.playbook.loader import Playbook, get_playbook, positions_block

#: Weighted penalty per finding severity (adherence formula — ported from the old
#: ``review_service.synthesize``; "none"-severity findings are dropped before this runs).
_SEVERITY_WEIGHT = {"high": 5.0, "medium": 2.0, "low": 0.5}
#: One missing/unverifiable required clause weighs about as much as a high finding.
_ABSENT_REQUIRED_PENALTY = 5.0
_ADHERENCE_PENALTY_SCALE = 20.0
#: Rough "expected clause count" for a document of this many characters — normalizes the penalty
#: by document size (a long, proportionally-compliant document isn't driven toward 0) without
#: needing the deleted clause segmenter.
_CHARS_PER_EXPECTED_CLAUSE = 2000


@dataclass
class ModelChoice:
    classifier: str
    quick: str
    deep: str


def _default_models() -> ModelChoice:
    return ModelChoice(
        classifier=settings.model_classifier,
        quick=settings.model_quick,
        deep=settings.model_deep,
    )


def _make_gateway(
    model: str, api_key: str, *, provider_only: tuple[str, ...] = ()
) -> Gateway:
    adapter = OpenRouterAdapter(
        api_key,
        model,
        base_url=settings.openrouter_base_url,
        zdr_only=True,  # fail-closed (plan §6) — never disabled for a live review
        provider_only=provider_only,
        timeout_s=settings.provider_timeout_s,
    )
    return Gateway(adapter)


@dataclass
class Finding:
    id: int
    clause_type: str
    clause_heading: str
    severity: str  # high | medium | low
    title: str
    rationale: str
    span: str
    span_faithful: bool
    suggested_language: str
    change_type: str
    playbook_position: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "clause_type": self.clause_type,
            "clause_heading": self.clause_heading,
            "severity": self.severity,
            "title": self.title,
            "rationale": self.rationale,
            "span": self.span,
            "span_faithful": self.span_faithful,
            "suggested_language": self.suggested_language,
            "change_type": self.change_type,
        }


@dataclass
class CoverageReport:
    checked: list[str]
    absent_required: list[dict]  # [{"clause_type": str, "note": str}]

    def as_dict(self) -> dict:
        return {"checked": self.checked, "absent_required": self.absent_required}


@dataclass
class ReviewResult:
    doc_type: str
    our_side: str
    summary: str
    risk_tier: str  # green | yellow | red
    adherence_score: float
    counts: dict  # {"high": n, "medium": n, "low": n}
    findings: list[Finding]
    coverage: CoverageReport | None
    warnings: list[str] = field(default_factory=list)
    calls: list[LlmCallRecord] = field(default_factory=list)
    playbook_version: str = ""

    @property
    def input_tokens(self) -> int:
        return sum(c.prompt_tokens for c in self.calls)

    @property
    def output_tokens(self) -> int:
        return sum(c.completion_tokens for c in self.calls)

    @property
    def cost_usd(self) -> float:
        return round(sum(c.cost_usd for c in self.calls), 6)


def _verify_span(text: str, span: str) -> tuple[str, bool]:
    """The unweakened verbatim-substring gate (``engine.spans.repair_span``, deterministic
    recovery only — no fuzzy guessing) — a fabricated/hallucinated span is REJECTED
    (``span_faithful=False``), never silently trusted."""
    rep = repair_span(text or "", span or "", allow_fuzzy=False)
    return rep.span, rep.faithful


def _build_findings(raw: list[dict], text: str) -> list[Finding]:
    out: list[Finding] = []
    for i, f in enumerate(raw, start=1):
        severity = str(f.get("severity") or "low")
        if severity == "none":
            continue  # dropped before dedupe (plan §4.2)
        span, faithful = _verify_span(text, str(f.get("span") or ""))
        out.append(
            Finding(
                id=i,
                clause_type=str(f.get("clause_type") or "other"),
                clause_heading=str(f.get("clause_heading") or ""),
                severity=severity,
                title=str(f.get("title") or "Review finding"),
                rationale=str(f.get("rationale") or ""),
                span=span,
                span_faithful=faithful,
                suggested_language=str(f.get("suggested_language") or ""),
                change_type=str(f.get("change_type") or "modification"),
                playbook_position=str(f.get("playbook_position") or ""),
            )
        )
    return out


def _coverage_report(items: list[CoverageItemResult], text: str) -> CoverageReport:
    checked: list[str] = []
    absent: list[dict] = []
    for item in items:
        checked.append(item.clause_type)
        if item.status == "absent":
            absent.append(
                {
                    "clause_type": item.clause_type,
                    "note": item.note or "Clause not found.",
                }
            )
            continue
        # "present" but the cited span isn't actually in the document -> treat as unverified,
        # i.e. effectively absent (plan §4.2: "absent_required = coverage items absent OR with
        # an unfaithful span").
        _, faithful = _verify_span(text, item.span or "")
        if not faithful:
            absent.append(
                {
                    "clause_type": item.clause_type,
                    "note": "Model claimed this clause is present but its cited text could not "
                    "be verified in the document.",
                }
            )
    return CoverageReport(checked=checked, absent_required=absent)


def _risk_tier(findings: list[Finding], coverage: CoverageReport | None) -> str:
    has_high = any(f.severity == "high" for f in findings) or bool(
        coverage.absent_required if coverage else []
    )
    if has_high:
        return "red"
    if any(f.severity == "medium" for f in findings):
        return "yellow"
    return "green"


def _adherence_score(
    findings: list[Finding], coverage: CoverageReport | None, doc_len: int
) -> float:
    penalty = sum(_SEVERITY_WEIGHT.get(f.severity, 0.5) for f in findings)
    penalty += _ABSENT_REQUIRED_PENALTY * len(
        coverage.absent_required if coverage else []
    )
    expected_clauses = max(1, round(doc_len / _CHARS_PER_EXPECTED_CLAUSE))
    denom = max(expected_clauses, len(findings), 1)
    score = 100.0 - (penalty / denom) * _ADHERENCE_PENALTY_SCALE
    return max(0.0, min(100.0, round(score, 1)))


def run_review(
    text: str,
    mode: str,
    our_side: str,
    api_key: str,
    *,
    models: ModelChoice | None = None,
    playbook: Playbook | None = None,
) -> ReviewResult:
    """Run the classifier -> reviewer ‖ coverage -> merge pipeline (plan §4.2). ``mode`` is
    ``"quick"`` or ``"deep"``. Raises :class:`~app.ai.gateway.ProviderError` (fail-closed) if the
    reviewer agent fails; a classifier or coverage failure degrades gracefully instead."""
    if mode not in ("quick", "deep"):
        raise ValueError(f"unknown mode {mode!r}; expected 'quick' or 'deep'")
    models = models or _default_models()
    pb = playbook or get_playbook()
    pb_block = positions_block(pb)
    warnings: list[str] = []
    deep = mode == "deep"
    deep_provider_only = settings.openrouter_provider_only_deep_list

    with track_usage() as ledger:
        # 1. classifier — fail-soft.
        classifier_gw = _make_gateway(models.classifier, api_key)
        try:
            classification: ClassifierResult = run_classifier(classifier_gw, text)
        except ProviderError as exc:
            warnings.append(f"classifier_failed:{error_code_for(exc)}")
            classification = UNKNOWN_CLASSIFICATION

        our_side_final = (our_side or "").strip() or classification.our_side_guess

        # 2. reviewer ‖ coverage — parallel fan-out, usage ledger follows the worker threads.
        reviewer_model = models.deep if deep else models.quick
        reviewer_provider_only = deep_provider_only if deep else ()
        reviewer_gw = _make_gateway(
            reviewer_model, api_key, provider_only=reviewer_provider_only
        )

        def _run_reviewer() -> list[dict]:
            return run_reviewer(
                reviewer_gw,
                text,
                pb_block,
                pb.version,
                style="edit" if deep else "triage",
                our_side=our_side_final,
                doc_type=classification.doc_type,
            )

        coverage_items: list[CoverageItemResult] | None = None
        if deep:
            coverage_gw = _make_gateway(models.quick, api_key)

            def _run_coverage() -> list[CoverageItemResult]:
                return run_coverage(coverage_gw, text, pb, pb_block)

            with ThreadPoolExecutor(max_workers=2) as ex:
                reviewer_future = ex.submit(_ctx_copy(_run_reviewer))
                coverage_future = ex.submit(_ctx_copy(_run_coverage))
                raw_findings = (
                    reviewer_future.result()
                )  # fail-closed: exception propagates
                try:
                    coverage_items = coverage_future.result()
                except ProviderError as exc:
                    warnings.append(f"coverage_failed:{error_code_for(exc)}")
                    coverage_items = None
        else:
            raw_findings = _run_reviewer()  # fail-closed: exception propagates

        # 3. deterministic merge (no LLM).
        findings = _build_findings(raw_findings, text)
        findings = _dedupe(findings)
        for i, f in enumerate(
            findings, start=1
        ):  # renumber 1..N after dedupe drops entries
            f.id = i
        coverage = (
            _coverage_report(coverage_items, text)
            if coverage_items is not None
            else None
        )
        counts = {
            "high": sum(1 for f in findings if f.severity == "high"),
            "medium": sum(1 for f in findings if f.severity == "medium"),
            "low": sum(1 for f in findings if f.severity == "low"),
        }
        risk_tier = _risk_tier(findings, coverage)
        adherence = _adherence_score(findings, coverage, len(text))

        return ReviewResult(
            doc_type=classification.doc_type,
            our_side=our_side_final,
            summary=classification.one_line_summary,
            risk_tier=risk_tier,
            adherence_score=adherence,
            counts=counts,
            findings=findings,
            coverage=coverage,
            warnings=warnings,
            calls=list(ledger.calls),
            playbook_version=pb.version,
        )


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """Dedupe by (clause_heading, span) via :func:`app.agents.reviewer.merge_findings`, operating
    on the already-built :class:`Finding` objects (round-tripped through dicts for that helper)."""
    dicts = [
        {
            "clause_heading": f.clause_heading,
            "span": f.span,
            "title": f.title,
            "_orig": f,
        }
        for f in findings
    ]
    kept = merge_findings(dicts)
    return [d["_orig"] for d in kept]
