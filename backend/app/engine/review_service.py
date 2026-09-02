"""End-to-end review engine (provider-agnostic, DB-free).

WHAT SHIPS (both production tiers, set in routes_v1._run_engine):
    segment → align (deterministic, off-model — clause metadata only) → **whole-document
    structured review** (the sole finding source) → coverage (deep only — detects a DELETED
    required clause) → deterministic synthesis (risk tier + adherence).
``run_review`` is called with ``clause_pass=False, whole_doc=True, self_verify=False,
gw_verify_b=None, cross_clause=False`` — quick is the whole-doc read on Sonnet; deep adds
coverage on Opus. The whole-doc benchmark (2026-06-24) showed the per-clause fan-out added
~0 recall over the whole-doc pass at most of the cost + false positives, so it was retired.

EVAL/LEGACY-ONLY passes, reachable ONLY via flags production does NOT set (kept for the
benchmark harness + experiments, NOT on the shipped path):
    - per-clause findings (``clause_pass=True``) — T2 fan-out;
    - the T4 ensemble verify gate / single-provider re-rate (``gw_verify_b`` / ``self_verify=True``);
    - T3 cross-clause (``cross_clause=True``) and the T2.7 walk-away critic.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from app.ai.gateway import Gateway
from app.ai.usage_ledger import ctx_copy, track_usage
from app.engine.coverage_runner import CoverageReport, run_coverage
from app.engine.crossclause import run_crossclause
from app.engine.embed_align import NO_REPORT, ClauseMatchReport, precheck_absent
from app.engine.embeddings import EmbeddingProvider
from app.engine.findings import playbook_positions_block, run_findings
from app.engine.prompt_release import prompt_release_id
from app.engine.router import deterministic_overrides, run_router
from app.engine.spans import repair_span
from app.engine.verify import SEVERITY_ORDER, ChangeContext, ensemble_rate, single_rate
from app.engine.walkaway import merge_walkaway, run_walkaway_critic
from app.engine.wholedoc import merge_findings, run_wholedoc
from app.ingestion.parser import parse_document
from app.ingestion.segmenter import segment_clauses
from app.playbook.coverage import build_checklist
from app.playbook.release import playbook_release_id
from app.review.alignment import align_clauses

log = logging.getLogger("nda.engine")

SEVERITY_WEIGHT = {"high": 5.0, "medium": 2.0, "low": 0.5, "none": 0.0}


@dataclass
class ReviewResult:
    risk_tier: str  # green | yellow | red
    adherence_score: float
    perspective: str
    findings: list[dict]
    coverage: CoverageReport
    cross_clause_flags: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    routing: dict | None = None
    playbook_version: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    #: S1 (escalate-only): required checklist items an embedding pre-check thinks may be ABSENT.
    #: ADDITIVE advisory only — surfaced under the additive ``embed_precheck`` response key, NEVER
    #: written into ``coverage.absent_required`` (deep's authoritative net). Each item: clause_type,
    #: title, score.
    embed_precheck: list[dict] = field(default_factory=list)
    #: (#8) Which analysis components ran DEGRADED (provider fallback / verify error): any of
    #: "coverage", "router", "verify". Non-empty means the risk tier may be conservative — a RED
    #: driven by degradation is NOT the same as confirmed legal risk.
    degraded_components: list[str] = field(default_factory=list)
    #: (#2) The review mode label ("quick" / "deep") and whether a coverage (deleted-clause) pass
    #: actually executed — quick is a TRIAGE read that skips coverage, so a clean quick result is
    #: lower-confidence than a clean deep one.
    mode: str = ""
    coverage_ran: bool = False
    #: (#6) Provenance: the models / provider / prompt + playbook release ids that produced this
    #: review, for audit + reproducibility. Populated by run_review; empty on hand-built results.
    provenance: dict = field(default_factory=dict)


def _eff_sev(f: dict) -> str:
    """Effective severity = verified (if the T4 gate ran) else the raw T2 severity."""
    return f.get("verified_severity") or f.get("severity", "low")


#: Adherence-penalty scale. score = 100 - (Σ weighted penalty / document size) * SCALE.
ADHERENCE_PENALTY_SCALE = 20.0


def build_review_lens(perspective: str, our_role: str, paper_owner: str) -> str:
    """A role/perspective context block prepended to the analysis prompts.

    When the document is Amperesand's OWN one-way paper (Amperesand discloses), a
    one-sided, disclosing-party-favorable structure is INTENTIONAL — the engine must
    not flag lack of mutuality / reciprocity / asymmetry as a deviation. For
    counterparty or mutual paper we return an empty lens, preserving the default
    (recall-first, flag-every-deviation) behavior that protects Amperesand as recipient.
    """
    # Fire ONLY when the one-way disclosing structure the lens text asserts is actually
    # established. The second clause covers uncertain perspective on Amperesand's OWN
    # paper, but must NOT fire on detected MUTUAL paper — suppressing mutuality/asymmetry
    # findings there would lose recall on a genuinely mutual document.
    own_favorable = (
        perspective == "one_way" and our_role in ("disclosing", "both")
    ) or (
        paper_owner == "amperesand"
        and perspective in ("one_way", "unknown")
        and our_role in ("disclosing", "both", "unknown")
    )
    if not own_favorable:
        return ""
    return (
        "DOCUMENT CONTEXT: This is Amperesand's OWN one-way NDA on which Amperesand is the "
        "DISCLOSING party. A one-sided, disclosing-party-favorable structure is INTENTIONAL and "
        "CORRECT here. Do NOT flag lack of mutuality, absence of reciprocal obligations, "
        "asymmetry, or one-sidedness in Amperesand's favor as a finding — that is the design, not "
        "a defect. Strict receiving-party obligations, a broad confidentiality definition, narrow "
        "recipient carve-outs, and prior-written-consent gates on the recipient all FAVOR "
        "Amperesand and are GOOD. Unfilled bracketed placeholders (e.g. [insert date], [full "
        "name], [NRIC/Passport number]) are fill-in fields in a template, not defects. Flag a "
        "clause ONLY when it genuinely WEAKENS Amperesand's protection as the disclosing party or "
        "imposes an obligation or risk ON Amperesand. Judge severity by harm to Amperesand — "
        "never by deviation from a mutual template or by one-sidedness."
    )


def synthesize(
    findings: list[dict],
    coverage: CoverageReport,
    cross_flags: list[dict],
    *,
    clause_count: int = 0,
) -> tuple[str, float]:
    """Risk tier + adherence score.

    Adherence is NORMALIZED by document size (clause count) and capped at [0,100], so
    a long, proportionally-compliant NDA is not driven toward 0 and DEEP's extra recall
    passes cannot score below QUICK (the prior absolute penalty did both). Low-severity
    findings are advisory and excluded from the penalty; a missing core clause weighs
    more than a single missing carve-out. The risk tier is unchanged.
    """
    absent = coverage.absent_required
    penalty = sum(
        SEVERITY_WEIGHT.get(_eff_sev(f), 0.5) for f in findings if _eff_sev(f) != "low"
    )
    penalty += sum(3.0 if c.kind == "clause" else 1.0 for c in absent)
    penalty += sum(
        SEVERITY_WEIGHT.get(cf.get("severity", "low"), 0.5)
        for cf in cross_flags
        if cf.get("severity") != "low"
    )
    denom = max(clause_count, len(findings), 1)
    score = 100.0 - (penalty / denom) * ADHERENCE_PENALTY_SCALE
    score = max(0.0, min(100.0, round(score, 1)))

    has_high = (
        any(_eff_sev(f) == "high" for f in findings)
        or bool(absent)
        or any(cf.get("severity") == "high" for cf in cross_flags)
    )
    has_med = any(_eff_sev(f) == "medium" for f in findings) or any(
        cf.get("severity") == "medium" for cf in cross_flags
    )
    tier = "red" if has_high else ("yellow" if has_med else "green")
    return tier, score


def _ctx_for(f: dict) -> ChangeContext:
    return ChangeContext(
        clause=f.get("clause_heading", ""),
        baseline_excerpt=f.get("_template_text", ""),
        variant_excerpt=f.get("_incoming_text", ""),
        playbook_position=f.get("playbook_position", ""),
    )


# Whole-doc-pass findings have no aligned baseline clause, so re-rating them against an empty
# baseline is noise — they keep their (already careful) severity. The SAME applies to a recall-safe
# "Unreviewed clause" fallback placeholder: it carries no real baseline, so re-rating would downgrade
# it to 'none' and prune it — silently dropping the clause that could NOT be reviewed (false GREEN).
def _verifiable(f: dict, sevs) -> bool:
    return (
        f.get("severity") in sevs
        and not f.get("verified_severity")
        and f.get("source") not in ("wholedoc", "walkaway")
        and not f.get("fallback_used")
    )


# C1: cheap-first gate on the high-severity ensemble verify — a COST EXPERIMENT, default OFF.
# The Opus model panel chose the UNCONDITIONAL cross-provider ensemble ("ensemble verify on every
# high", decision #3) for the deep / highest-quality tier; on the available data the gate did not
# cut cost, so it is not the default. It stays available behind NDA_C1_GATE=1 for the sanctioned
# single-Opus-re-rate hybrid experiment, not as shipped behaviour.
_C1_GATE = os.environ.get("NDA_C1_GATE", "0") != "0"


def _verify_high_findings(
    gw,
    gw_verify_b,
    gw_deep,
    findings,
    playbook_version,
    *,
    eval_mode,
    max_workers: int = 16,
    lens: str = "",
    gate: bool = _C1_GATE,
) -> float:
    """T4: verify high-severity findings — C1-gated (was: unconditional ensemble, decision #3).

    The full ensemble (Sonnet finder + Haiku verify, Opus tiebreak on disagreement) is the
    cost driver on high-heavy documents (it fired on every high — up to ~3 calls each). C1 gates
    it cheap-first: EVERY high gets ONE re-rate on the cheap verify provider; if that re-rate
    CONFIRMS the high (or rates it higher), it is kept in a single call (finder + verifier already
    agree). Only a candidate DOWNGRADE escalates to the full ensemble — never trust one model to
    DROP a recall-first high. Every downgrade is therefore still ensemble-confirmed (recall
    preserved); the saving is on the confirmed highs, which are the majority on high-heavy docs.
    Set NDA_C1_GATE=0 to restore the unconditional ensemble (decision #3) for an A/B baseline.
    """
    targets = [f for f in findings if _verifiable(f, ("high",))]
    if not targets:
        return 0.0

    def ensemble(f) -> float:
        v = ensemble_rate(
            gw,
            gw_verify_b,
            _ctx_for(f),
            playbook_version,
            gw_deep=gw_deep,
            effort="low",
            eval_mode=eval_mode,
            lens=lens,
        )
        f["verify"] = {
            "severity": v.severity,
            "agreed": v.agreed,
            "escalated": v.escalated,
            "deciding": v.deciding,
            "mode": "ensemble",
        }
        f["verified_severity"] = v.severity
        return sum((rec.get("cost_usd") or 0.0) for rec in v.per_provider.values())

    def work(f):
        try:
            if gate and gw_verify_b is not None:
                # Cheap-first re-rate on the verify provider (cheapest model) for EVERY high.
                d = single_rate(
                    gw_verify_b,
                    _ctx_for(f),
                    playbook_version,
                    effort="low",
                    eval_mode=eval_mode,
                    lens=lens,
                )
                if (
                    SEVERITY_ORDER.get(d.get("severity") or "", 0)
                    >= SEVERITY_ORDER["high"]
                ):
                    f["verify"] = {
                        "severity": d["severity"],
                        "mode": "single-gated",
                        "confidence": d.get("confidence"),
                    }
                    f["verified_severity"] = d["severity"]
                    return d.get("cost_usd") or 0.0
                # The cheap pass wants to DOWNGRADE — don't trust one model; confirm via the ensemble.
                return (d.get("cost_usd") or 0.0) + ensemble(f)
            return ensemble(f)
        except Exception as e:  # noqa: BLE001
            f["verify"] = {"error": type(e).__name__}
            return 0.0

    # ctx_copy: propagate the caller's usage ledger (run_review's track_usage) into the workers.
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        return sum(ex.map(ctx_copy(work), targets))


def _verify_high_findings_single(
    gw,
    findings,
    playbook_version,
    *,
    effort,
    eval_mode,
    sevs=("high",),
    max_workers: int = 16,
    lens: str = "",
) -> float:
    """T4-lite: re-rate findings of the given severities on a SINGLE provider, in parallel.

    A cheap precision pass — one call per finding, no second provider — that kills
    over-eager findings the recall-first T2 pass produces (highs in quick mode, mediums
    in both modes). Skips findings already ensemble-rated or from the whole-doc pass.
    """
    targets = [f for f in findings if _verifiable(f, sevs)]
    if not targets:
        return 0.0

    def work(f):
        try:
            d = single_rate(
                gw,
                _ctx_for(f),
                playbook_version,
                effort=effort,
                eval_mode=eval_mode,
                lens=lens,
            )
            f["verify"] = {
                "severity": d["severity"],
                "mode": "single",
                "confidence": d.get("confidence"),
            }
            f["verified_severity"] = d["severity"]
            return d.get("cost_usd") or 0.0
        except Exception as e:  # noqa: BLE001
            f["verify"] = {"error": type(e).__name__}
            return 0.0

    # ctx_copy: propagate the caller's usage ledger (run_review's track_usage) into the workers.
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        return sum(ex.map(ctx_copy(work), targets))


def run_review(
    gw: Gateway,
    *,
    incoming_text: str,
    standard_text: str,
    playbook: dict,
    playbook_version: str,
    gw_verify_b: Gateway | None = None,
    gw_deep: Gateway | None = None,
    gw_router: Gateway | None = None,
    checklist=None,
    effort: str = "medium",
    eval_mode: bool = False,
    drop_cosmetic: bool = True,
    cross_clause: bool = True,
    self_verify: bool = False,
    whole_doc: bool = False,
    clause_pass: bool = True,
    wholedoc_style: str | None = None,
    redlines: bool = False,
    skip_coverage: bool = False,
    unchanged_threshold: float | None = None,
    router_obj: dict | None = None,
    router_degraded: bool = False,
    prior_cost: float = 0.0,
    prior_input_tokens: int = 0,
    prior_output_tokens: int = 0,
    profile: str = "deep",
    #: (#2) Mode label ("quick"/"deep") echoed into the result + response for the caller-supplied
    #: mode. A caller that pre-ran the router (routes_v1) also passes ``router_degraded`` so its
    #: pre-run fallback is recorded here even though run_review makes no router call of its own.
    mode_label: str = "",
    # Escalate-only embedding signals (all ADDITIVE; default off -> byte-identical behavior).
    # `clause_match` (S2): its trigger_hits seed the whole-doc risk-area hints. `clause_sim` (S4):
    # an embedding cosine that refines clause pairing (else difflib). `embed_provider` (S1): the
    # deletion pre-check embedder — quick + whole scope only (deep already runs coverage).
    clause_match: ClauseMatchReport | None = None,
    clause_sim: Callable[[str, str], float] | None = None,
    embed_provider: EmbeddingProvider | None = None,
) -> ReviewResult:
    # Per-review token attribution: a REQUEST-SCOPED ledger (app.ai.usage_ledger).
    # Gateways are lru-cached and SHARED across concurrent reviews, so an entry/exit
    # delta over their process-wide counters would absorb a concurrent review's calls.
    # track_usage() installs a contextvar ledger that Gateway.run feeds on every REAL
    # provider call (cache hits and fallbacks add nothing); every ThreadPoolExecutor
    # fan-out below wraps its callable in ctx_copy() so worker threads report into the
    # SAME ledger. NOTE: an inner track_usage would shadow this one — none is opened
    # inside the review passes.
    # `prior_*` fold in work a caller did before run_review — e.g. routes_v1 pre-runs the
    # T0 router to pick the v4 playbook, then passes its router_obj + cost/tokens here.
    with track_usage() as _ledger:
        # T0 router: perspective / role / paper-owner + deterministic deep-path guardrails (P1-4).
        # A caller (routes_v1) that already classified the doc (to pick the per-variant playbook)
        # passes its verdict as `router_obj` so the SAME classification drives the lens — no second call.
        routing = None
        perspective = "mutual"
        our_role = "unknown"
        paper_owner = "unknown"
        router_cost = 0.0
        # (#8) A caller may hand us a pre-run router degradation flag (routes_v1 pre-runs the router to
        # pick the variant); OR run_review makes its own router call below and learns it directly.
        router_ran_degraded = router_degraded
        robj = router_obj
        if robj is None and gw_router is not None:
            robj, router_cost, _internal_router_degraded = run_router(
                gw_router, incoming_text, playbook_version, eval_mode=eval_mode
            )
            router_ran_degraded = router_ran_degraded or _internal_router_degraded
        if robj is not None:
            perspective = robj.get("perspective") or "mutual"
            our_role = robj.get("our_role") or "unknown"
            paper_owner = robj.get("paper_owner") or "unknown"
            routing = {**deterministic_overrides(robj, incoming_text), "router": robj}
        # Perspective lens: on Amperesand's OWN one-way paper, suppress one-sidedness findings;
        # empty (the default) preserves counterparty/mutual recall-first behavior.
        lens = build_review_lens(perspective, our_role, paper_owner)

        incoming_clauses = segment_clauses(incoming_text)
        standard_clauses = segment_clauses(standard_text)
        pairs = align_clauses(
            standard_clauses,
            incoming_clauses,
            unchanged_threshold=unchanged_threshold,
            text_sim_fn=clause_sim,  # S4: embedding cosine when on; None -> difflib (identical)
        )

        # S2 — walk-away-proximity hints: the top-k (<=5) trigger resemblances become non-authoritative
        # attention pointers appended to the VOLATILE whole-doc task. Never enters the stable prefix.
        risk_hints: list[str] = []
        cm = clause_match or NO_REPORT
        if cm.trigger_hits:
            top = sorted(cm.trigger_hits, key=lambda h: h[2], reverse=True)[:5]
            risk_hints = [
                f"clause {idx + 1} resembles: {trigger}"
                for idx, trigger, _score in top
                if trigger
            ]

        # T2 per-clause findings, T2.5 whole-document recall, and T1.6 coverage are INDEPENDENT
        # passes (no data dependency), so run them CONCURRENTLY — wall-clock becomes max(...) not
        # sum(...). Each pass manages its own internal fan-out; the gateway is thread-safe (locked
        # usage counter + response cache). Identical calls/tokens/outputs — pure scheduling win.
        # Q1: the walk-away completeness critic (T2.7) runs on the cheap verify gateway (Haiku in
        # deep). It is a deep-only recall floor, so it is enabled only when that gateway exists and
        # NDA_Q1_CRITIC != 0. It joins the concurrent fan-out below, so it costs ~no extra wall-clock.
        _q1 = (gw_verify_b is not None) and (
            os.environ.get("NDA_Q1_CRITIC", "1") != "0"
        )
        with ThreadPoolExecutor(max_workers=4) as _ex:
            # Quick tier (clause_pass=False) skips the per-clause finding fan-out — the dominant cost —
            # and relies on the single whole-doc recall call alone (a structured "1-shot" review).
            _f_find = (
                _ex.submit(
                    ctx_copy(run_findings),  # carry the usage ledger into the worker
                    gw,
                    pairs,
                    playbook,
                    playbook_version,
                    effort=effort,
                    eval_mode=eval_mode,
                    drop_cosmetic=drop_cosmetic,
                    lens=lens,
                    profile=profile,
                )
                if clause_pass
                else None
            )
            _f_wd = (
                _ex.submit(
                    ctx_copy(run_wholedoc),  # carry the usage ledger into the worker
                    gw_deep or gw,
                    standard_text,
                    incoming_text,
                    playbook,
                    playbook_version,
                    playbook_block=playbook_positions_block(playbook),
                    effort=effort,
                    eval_mode=eval_mode,
                    lens=lens,
                    profile=profile,
                    style=wholedoc_style,
                    redlines=redlines,
                    risk_hints=risk_hints,
                )
                if whole_doc
                else None
            )
            # Coverage checks REQUIRED clauses present vs the playbook checklist. The redlines-only scope
            # passes skip_coverage=True: the baseline is the doc's own ORIGINAL (not the standard template),
            # so a checklist sweep would flag pre-existing gaps rather than the changes under review.
            _f_cov = (
                _ex.submit(
                    ctx_copy(run_coverage),  # carry the usage ledger into the worker
                    gw,
                    checklist or build_checklist(playbook),
                    incoming_text,
                    playbook_version,
                    eval_mode=eval_mode,
                    lens=lens,
                )
                if not skip_coverage
                else None
            )
            _f_wa = (
                _ex.submit(
                    ctx_copy(
                        run_walkaway_critic
                    ),  # carry the usage ledger into the worker
                    gw_verify_b,
                    playbook,
                    incoming_text,
                    playbook_version,
                    effort="low",
                    eval_mode=eval_mode,
                    lens=lens,
                )
                # _q1 already implies gw_verify_b is not None (see its definition); restating it here lets
                # the type checker narrow gw_verify_b to Gateway for the submit — same runtime condition.
                if (gw_verify_b is not None and _q1)
                else None
            )
            findings = _f_find.result() if _f_find else []
            try:
                wd, wd_cost = _f_wd.result() if _f_wd else ([], 0.0)
            except Exception:
                # When clause_pass=False (EVERY production tier) the whole-doc pass is the SOLE
                # deviation-finding source: a hard failure must NEVER be reported as a clean review, so
                # propagate it and let the API surface an error. Only when a per-clause pass is also
                # running is the whole-doc pass a best-effort recall booster we can safely drop.
                if not clause_pass:
                    raise
                log.exception(
                    "whole-doc booster pass failed; continuing on per-clause findings"
                )
                wd, wd_cost = [], 0.0
            coverage = _f_cov.result() if _f_cov else CoverageReport(findings=[])
            wa, wa_cost = _f_wa.result() if _f_wa else ([], 0.0)
        # Recover deviations the per-clause alignment washes out; per-clause findings preferred on overlap.
        if whole_doc:
            findings = merge_findings(findings, wd)

        # S1 — quick-tier deletion pre-check (quick == skip_coverage & whole scope; deep runs the
        # authoritative coverage pass, redlines has no template baseline). Any REQUIRED checklist item
        # whose best embedding cosine against the incoming clauses is below the floor becomes ONE
        # ADVISORY finding recommending a deep review. It is purely additive: it appends a finding and
        # populates the additive `embed_precheck` key — it NEVER prunes a finding or feeds coverage.
        precheck_out: list[dict] = []
        if embed_provider is not None and skip_coverage and not redlines:
            for cand in precheck_absent(
                incoming_text, checklist or build_checklist(playbook), embed_provider
            ):
                precheck_out.append(
                    {
                        "clause_type": cand.clause_type,
                        "title": cand.title,
                        "score": round(cand.score, 4),
                    }
                )
                # span "" -> repair_span returns faithful=False, so it flows through the pipeline with
                # no Apply affordance (never a tracked edit) — the whole-doc empty-span convention.
                findings.append(
                    {
                        "clause_heading": "",
                        "clause_types": [cand.clause_type] if cand.clause_type else [],
                        "change_type": "absent",
                        "severity": "medium",
                        "title": f"Required clause possibly absent: {cand.title}",
                        "rationale": (
                            "Automated embedding pre-check: no clause in the document closely matches this "
                            "required position. This is a non-authoritative signal — run a deep review to "
                            "confirm whether the clause is genuinely absent."
                        ),
                        "suggested_language": "",
                        "span": "",
                        "span_faithful": repair_span(incoming_text or "", "").faithful,
                        "fallback_used": False,
                        "cost_usd": 0.0,
                        "source": "embed_precheck",
                    }
                )
        # Sum per-finding API cost BEFORE pruning model-declared 'none' findings — those were real paid
        # calls, so dropping them from the cost would make cost_usd disagree with the token totals.
        findings_cost = sum((f.get("cost_usd") or 0.0) for f in findings)
        # Drop model-declared non-issues (cosmetic formatting the deterministic filter missed).
        findings = [f for f in findings if f.get("severity") != "none"]

        cost = (
            prior_cost
            + router_cost
            + wd_cost
            + wa_cost
            + (coverage.cost_usd or 0.0)
            + findings_cost
        )
        if (
            gw_verify_b is not None
        ):  # deep: ensemble on highs (recall-critical) + cheap single re-rate on mediums+lows
            cost += _verify_high_findings(
                gw,
                gw_verify_b,
                gw_deep,
                findings,
                playbook_version,
                eval_mode=eval_mode,
                lens=lens,
            )
            # Q2: precision re-rate now covers LOWs too, so a meaning-preserving reword the recall-first
            # T2 pass rated 'low' (a probe false-positive) gets downgraded to 'none' and pruned below.
            cost += _verify_high_findings_single(
                gw,
                findings,
                playbook_version,
                effort=effort,
                eval_mode=eval_mode,
                sevs=("medium", "low"),
                lens=lens,
            )
        elif (
            self_verify
        ):  # quick: single-provider re-rate (high-only for the lean quick profile)
            _sevs = ("high",) if profile == "quick" else ("high", "medium")
            cost += _verify_high_findings_single(
                gw,
                findings,
                playbook_version,
                effort=effort,
                eval_mode=eval_mode,
                sevs=_sevs,
                lens=lens,
            )

        # Re-prune on EFFECTIVE severity: the verify gate may downgrade a finding to 'none'
        # (a confirmed non-issue) — drop it so it neither shows nor feeds cross-clause.
        findings = [f for f in findings if _eff_sev(f) != "none"]

        # Q1 recall floor: add walk-away triggers on clauses the other passes MISSED — AFTER verify+prune,
        # so coverage is computed against the findings that actually SURVIVE (a covering finding the
        # verify pass downgraded to 'none' no longer suppresses its walk-away), and the checklist-grounded
        # walk-aways are never themselves sent through the baseline-less re-rate that could prune them.
        if wa:
            findings = merge_walkaway(findings, wa)

        cross_flags: list[dict] = []
        if cross_clause:
            # C2: run cross-clause on the PRIMARY gateway (Sonnet in deep), not the Opus deep
            # tiebreaker. Measured +0.000 recall vs no cross-clause, so the Opus premium here buys
            # nothing — the cheaper primary preserves the cross-clause flags (tier/adherence) for less.
            cross_flags, cc_cost = run_crossclause(
                gw, findings, playbook_version, eval_mode=eval_mode
            )
            cost += cc_cost

        tier, adherence = synthesize(
            findings,
            coverage,
            cross_flags,
            clause_count=max(len(incoming_clauses), len(standard_clauses)),
        )
        # Total tokens used THIS review = this review's ledger (fed by every real
        # provider call above, across every fan-out thread via ctx_copy) + any work
        # the caller did before run_review (prior_*).
        in_tokens = prior_input_tokens + _ledger.input_tokens
        out_tokens = prior_output_tokens + _ledger.output_tokens
        counts = {
            "findings": len(findings),
            "high": sum(1 for f in findings if _eff_sev(f) == "high"),
            "medium": sum(1 for f in findings if _eff_sev(f) == "medium"),
            "low": sum(1 for f in findings if _eff_sev(f) == "low"),
            "downgraded_by_verify": sum(
                1
                for f in findings
                if f.get("verified_severity")
                and SEVERITY_ORDER.get(f["verified_severity"], 1)
                < SEVERITY_ORDER.get(f.get("severity") or "", 1)
            ),
            "escalated_by_verify": sum(
                1
                for f in findings
                if f.get("verified_severity")
                and SEVERITY_ORDER.get(f["verified_severity"], 1)
                > SEVERITY_ORDER.get(f.get("severity") or "", 1)
            ),
            "verify_failed": sum(
                1
                for f in findings
                if isinstance(f.get("verify"), dict) and f["verify"].get("error")
            ),
            "absent_required": len(coverage.absent_required),
            "cross_clause_flags": len(cross_flags),
            "unfaithful_spans": sum(
                1 for f in findings if f.get("span_faithful") is False
            ),
            "fallbacks": sum(1 for f in findings if f.get("fallback_used")),
            "walkaway_added": sum(1 for f in findings if f.get("source") == "walkaway"),
        }
        # (#8) Which components ran DEGRADED — surfaced so a conservative (often RED) tier caused by a
        # provider fallback is distinguishable from confirmed legal risk. coverage: its gateway fell
        # back; router: the router call (pre-run or internal) fell back; verify: a verify re-rate errored.
        degraded_components: list[str] = []
        if coverage.degraded:
            degraded_components.append("coverage")
        if router_ran_degraded:
            degraded_components.append("router")
        if counts.get("verify_failed", 0) > 0:
            degraded_components.append("verify")

        # (#2) Whether a coverage (deleted-clause) pass actually executed this review — quick skips it.
        coverage_ran = not skip_coverage

        # (#6) Provenance: models / provider / prompt + playbook release ids. Defensive getattr chains —
        # test fakes may not carry a full adapter, and the router/deep gateways are optional.
        def _model_id(g) -> str:
            return getattr(getattr(g, "adapter", None), "model_id", "") or ""

        provenance = {
            "models": {
                "primary": _model_id(gw),
                "router": _model_id(gw_router) if gw_router is not None else "",
                "deep": _model_id(gw_deep) if gw_deep is not None else "",
            },
            "provider": getattr(getattr(gw, "adapter", None), "name", "") or "",
            "prompt_release": prompt_release_id(),
            "playbook_release": playbook_release_id(),
            "mode": mode_label,
        }

        return ReviewResult(
            tier,
            adherence,
            perspective,
            findings,
            coverage,
            cross_flags,
            counts,
            round(cost, 4),
            routing,
            playbook_version,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            embed_precheck=precheck_out,
            degraded_components=degraded_components,
            mode=mode_label,
            coverage_ran=coverage_ran,
            provenance=provenance,
        )


def run_review_files(
    gw: Gateway, *, incoming_path: str, standard_path: str, **kw
) -> ReviewResult:
    incoming = parse_document(incoming_path).full_text
    standard = parse_document(standard_path).full_text
    return run_review(gw, incoming_text=incoming, standard_text=standard, **kw)
