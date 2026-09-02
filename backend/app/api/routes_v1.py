"""`/v1` — the single review API every channel calls (Word add-in, n8n, Slack, email).

Wraps the provider-neutral engine (``engine.review_service.run_review``) behind a
stable HTTP contract: upload a document, get back a structured review (risk tier,
findings, coverage gaps, cross-clause flags) plus a provider-neutral ``redline_plan``
(find / replace / comment) the channels apply as Word tracked changes.

v1 is synchronous and persists reviews to the database (via ``reviews_repo``) so
history survives process restarts. Auth is FAIL-CLOSED via ``engine_principal``: every request
resolves a WEB (cookie) / SERVICE (``X-API-Key``) principal or is denied, then
``require_engine_action`` checks the per-mode entitlement (see ``docs/ARCHITECTURE.md`` →
Auth model). Errors use one envelope: ``{"error": {...}}``.

Endpoints in this file (search the ``@router`` decorators):
    POST /v1/reviews                     create_review  — the spine: upload → review
    GET  /v1/reviews                     list_reviews   — org-scoped history
    GET  /v1/reviews/{id}                get_review     — one stored review
    POST /v1/redline                     redline        — the stored review's redline plan
    GET  /v1/reviews/{id}/redline.docx   redline_docx   — tracked-changes .docx download

NOTE on the comment shorthand: markers like ``P1-1``, ``P1-6``, ``C4``, ``T0`` are HISTORICAL
hardening-ticket references (security/quality work), not runtime concepts — see the glossary in
``docs/ARCHITECTURE.md``. The full request flow is mapped there too.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
import threading
import uuid
from functools import lru_cache, partial
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from app.ai.gateway import Gateway
from app.ai.usage_ledger import track_usage
from app.api import reviews_repo
from app.api.schemas_v1 import RedlineResponse, ReviewResponse
from app.api.uploads import guard_zip_bomb as _guard_zip_bomb
from app.config import settings
from app.engine import simcache
from app.engine.embed_align import NO_REPORT, embed_and_match, make_clause_sim
from app.engine.embeddings import get_provider, load_index
from app.engine.review_service import ReviewResult, run_review
from app.engine.router import run_router
from app.ingestion.parser import parse_document
from app.playbook.coverage import (
    PlaybookValidationError,
    load_playbook,
    validate_v4_manifest,
)
from app.redline.docx_writer import build_redlined_docx

log = logging.getLogger("nda.v1")
router = APIRouter(prefix="/v1", tags=["v1"])

_ALLOWED_SUFFIXES = {".docx", ".pdf", ".txt", ".md", ".markdown", ".text", ".doc"}
_ALLOWED_CHANNELS = {"api", "word", "n8n", "slack", "email"}
_REVIEW_ID_RE = re.compile(r"[0-9a-f]{32}")  # uuid4().hex shape

# ---------------------------------------------------------------------------- #
# Review-concurrency ceiling (settings.review_concurrency, previously declared
# but never applied). Bounds how many PAID engine runs execute at once in THIS
# process — a burst beyond it gets a typed 429 instead of stacking threadpool
# work until the DB pool (db.py: 15+15) exhausts and /healthz's own SELECT 1
# turns a busy replica into a restarting one. Cache-hit/extract paths are never
# gated (they cost nothing). NOTE: per-process — with N api replicas the global
# ceiling is N×review_concurrency; the worker's async claimer applies the same
# setting as its own independent budget.
# ---------------------------------------------------------------------------- #
_review_slots: threading.BoundedSemaphore | None = None
_review_slots_lock = threading.Lock()


def _review_semaphore() -> threading.BoundedSemaphore:
    """The process-wide engine-run semaphore, built lazily so tests can reset
    ``_review_slots = None`` after monkeypatching ``settings.review_concurrency``."""
    global _review_slots
    if _review_slots is None:
        with _review_slots_lock:
            if _review_slots is None:
                _review_slots = threading.BoundedSemaphore(
                    max(1, int(getattr(settings, "review_concurrency", 3) or 3))
                )
    return _review_slots


def acquire_review_slot() -> threading.BoundedSemaphore:
    """Non-blocking try-acquire of an engine-run slot; raises the typed 429 at capacity.
    The caller MUST release the returned semaphore in a ``finally``. Shared by the sync
    /v1/reviews path and (as the same per-process bound) any in-process runner."""
    sem = _review_semaphore()
    if not sem.acquire(blocking=False):
        raise EngineError(
            429,
            "review_capacity",
            "The review engine is at capacity; retry shortly.",
            {"max_concurrent": max(1, int(settings.review_concurrency or 3))},
            headers={"Retry-After": "15"},
        )
    return sem


def _safe_filename_title(title: str) -> str:
    """A download-safe title for Content-Disposition: drop control/quote chars and
    clamp length. Defense-in-depth — Starlette already percent-encodes, but the title
    comes from model-generated counterparty text, so we never pass raw control bytes."""
    cleaned = re.sub(r'[\r\n\t"\\/]+', " ", title or "").strip()
    return cleaned[:100]


_REPO = (
    Path(__file__).resolve().parents[3]
)  # NDA Review/ (api/ -> app/ -> backend/ -> repo)
_DEFAULT_PLAYBOOK = (
    _REPO / "playbook" / "playbook_nda_v3.json"
)  # legacy fallback (v3, single merged)
_DEFAULT_STANDARD = (
    _REPO / "samples" / "nda-eval" / "template" / "amperesand_standard_nda.md"
)
_V4_MANIFEST = _REPO / "playbook" / "v4" / "manifest.json"  # per-variant registry (v4)


@lru_cache(maxsize=1)
def _v4_manifest() -> dict | None:
    """The v4 per-variant registry (variant_key -> playbook + baseline paths), or None."""
    if not _V4_MANIFEST.exists():
        return None
    try:
        return json.loads(_V4_MANIFEST.read_text())
    except Exception:  # noqa: BLE001 — a malformed manifest must not break reviews
        log.exception("failed to load v4 manifest at %s", _V4_MANIFEST)
        return None


def select_variant_playbook(router_obj: dict) -> dict | None:
    """Map a T0 router verdict -> a v4 playbook entry {variant_key, playbook, baseline, ...}.

    Routes by (jurisdiction x counterparty x mutuality). FAIL-SAFE: if the document is not
    an NDA, the router is low-confidence, or the jurisdiction/counterparty can't be pinned,
    fall back to the most-protective Company-mutual variant for the jurisdiction (full
    recall, mutual baseline) so scrutiny is never silently relaxed. Returns None when no v4
    manifest is available (caller then uses the legacy default playbook).
    """
    man = _v4_manifest()
    if not man:
        return None
    # Fail loudly (not silently to the legacy playbook) on a structurally broken manifest:
    # inconsistent entries or a referenced playbook/baseline missing on disk. Surfaced as the
    # same 503 as a missing playbook file so the error envelope stays consistent.
    try:
        validate_v4_manifest(man, _REPO)
    except PlaybookValidationError as exc:
        raise EngineError(503, "playbook_unavailable", str(exc)) from exc
    by_key = {b["variant_key"]: b for b in man.get("playbooks", [])}
    fb = man.get("fail_safe_default", {}) or {}
    jur = {"sg": "SG", "us": "US"}.get((router_obj.get("jurisdiction") or "").lower())
    cp = (router_obj.get("counterparty_type") or "unknown").lower()
    persp = (router_obj.get("perspective") or "unknown").lower()
    conf = (router_obj.get("confidence") or "low").lower()
    owner = (router_obj.get("paper_owner") or "unknown").lower()

    def failsafe() -> dict | None:
        return by_key.get(fb.get(jur or "unknown") or fb.get("unknown") or "US_Company")

    # Fail safe to the most-protective Company-mutual variant (never silently relax
    # scrutiny) on: non-NDA, low confidence, unpinnable jurisdiction, OR external (not
    # Amperesand-owned) paper — the manifest selection_policy's stated invariant.
    if (
        not router_obj.get("is_nda", True)
        or conf == "low"
        or jur is None
        or owner in ("counterparty", "third_party")
    ):
        return failsafe()
    if cp == "company":
        return by_key.get(f"{jur}_Company") or failsafe()
    if cp == "service_provider":
        return by_key.get(f"{jur}_Service_Provider") or failsafe()
    if cp == "individual":
        mut = {"one_way": "Unilateral", "mutual": "Mutual"}.get(persp)
        return (
            (by_key.get(f"{jur}_Individual_{mut}") or failsafe()) if mut else failsafe()
        )
    return failsafe()


# --------------------------------------------------------------------------- #
# Error envelope (moved to app.api.errors so the auth deps can raise it without a cycle)
# --------------------------------------------------------------------------- #
from app.api.errors import (  # noqa: E402,F401  (re-export)
    EngineError,
    engine_error_handler,
)
from app.auth.entitlement import NotEntitled, require_engine_entitlement  # noqa: E402
from app.auth.principal import (  # noqa: E402
    ResolvedPrincipal,
    engine_principal,
    require_engine_action,
)

# P1-1: the HARD engine gate. ``engine_principal`` resolves a WEB / SERVICE principal (or
# DENIES 403 — no fail-open) and enforces the per-key request rate. The per-ACTION entitlement check
# is done in the route once the review mode is known (``require_engine_action``); reads only need a
# resolved principal. ``resolve_principal`` is kept as an alias so existing route signatures + tests
# referencing it keep working.
resolve_principal = engine_principal


# --------------------------------------------------------------------------- #
# Engine wiring (overridable in tests)
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=8)
def _build_gateways(
    mode: str, a: str, deep_1h_cache: bool = False, openrouter_key: str = ""
) -> dict:
    """Locked production configs, cached by (mode, keys) so the underlying HTTP connection pools
    + response cache persist across requests. Two whole-doc-centric tiers (the per-clause fan-out
    was dropped — the 2026-06-24 benchmark showed it added ~0 recall over the whole-doc pass while
    contributing most of the cost + false positives):
      quick -> single whole-doc structured review on Sonnet (+ Haiku router). The workhorse.
      deep  -> whole-doc minimal-edit review on OPUS 4.8 + coverage (+ Haiku router): the strongest
               reviewer, plus coverage to detect a DELETED / missing required clause.
    (A `max` tier — 2nd Opus whole-doc read + cross-clause — was evaluated and dropped: not better
    than deep at 2.5x the cost.)

    Adapter selection (PLAN §2/§3.8): when an OpenRouter key is configured, every tier rides the
    ZDR-pinned OpenRouter adapter with the vendor-namespaced model ids from config (same
    {primary, router} structure, same Opus/Sonnet/Haiku model choices). Otherwise the ported
    direct-Anthropic adapter serves as the configuration fallback. Neither key -> 503 no_provider.
    """
    from app.ai.adapters import AnthropicAdapter
    from app.ai.openrouter import OpenRouterAdapter

    timeout = float(getattr(settings, "provider_timeout_s", 150.0))
    # deep_1h_cache (config.prompt_cache_1h_deep): the deep PRIMARY prefix gets the extended 1h
    # cache TTL (2x write cost, accounted in pricing) — the router (Haiku) always stays 5m.
    primary_ttl = "1h" if (deep_1h_cache and mode == "deep") else "5m"

    if openrouter_key:
        # OpenRouter primary. The adapter takes the timeout at construction (its own httpx client),
        # so no with_options wrapping is needed here.
        def orgw(
            model: str,
            cache_ttl: str = "5m",
            provider_only: tuple[str, ...] | None = None,
        ) -> Gateway:
            return Gateway(
                OpenRouterAdapter(
                    openrouter_key,
                    model,
                    base_url=settings.openrouter_base_url,
                    zdr_only=bool(settings.openrouter_zdr_only),
                    provider_only=(
                        provider_only
                        if provider_only is not None
                        else settings.openrouter_provider_only_list
                    ),
                    timeout_s=timeout,
                    cache_ttl=cache_ttl,
                )
            )

        # Deep primary (opus-4-8) pins to its own provider (Vertex) — Bedrock 400s json_schema for
        # opus. Quick primary + router keep the global pin (they route fine by default).
        if mode == "deep":
            primary_gw = orgw(
                settings.openrouter_model_review_deep,
                primary_ttl,
                provider_only=settings.openrouter_provider_only_deep_list,
            )
        else:
            primary_gw = orgw(settings.openrouter_model_review_quick, primary_ttl)
        return {
            "primary": primary_gw,
            "router": orgw(settings.openrouter_model_router),
        }

    if not a:
        raise EngineError(503, "no_provider", "No AI provider API key is configured.")

    # C4: apply a per-call provider timeout HERE, at construction, leaving the adapter class
    # itself untouched. The SDK default read timeout is 600s, so one hung connection can stall a
    # review for minutes (a 715s outlier was seen); with_options returns a client copy whose httpx
    # timeout is bounded, and on timeout the SDK raises APITimeoutError -> RetryableProviderError so
    # the gateway retries before falling back. Override via PROVIDER_TIMEOUT_S; set above the
    # slowest legitimate call (large-doc Opus whole-doc reads) so it trims the tail, not real work.
    def _timed(adapter):
        try:
            adapter._client = adapter._client.with_options(timeout=timeout)
        except Exception:  # noqa: BLE001 — timeout wiring must never break gateway construction
            pass
        return adapter

    def an(model: str, cache_ttl: str = "5m") -> Gateway:
        return Gateway(_timed(AnthropicAdapter(a, model, cache_ttl=cache_ttl)))

    # Deep uses Opus 4.8 as the reviewer (whole-doc minimal-edit + coverage); quick stays on Sonnet.
    primary = "claude-opus-4-8" if mode == "deep" else "claude-sonnet-4-6"
    return {"primary": an(primary, primary_ttl), "router": an("claude-haiku-4-5")}


def build_engine_gateways(cfg, *, mode: str) -> dict:
    # The OpenRouter key is env-only (config.Settings) — deliberately NOT a settings_store override
    # key like the Anthropic one, so the ZDR-pinned primary can't be swapped out from the UI.
    return _build_gateways(
        mode,
        cfg.anthropic_api_key or "",
        bool(getattr(settings, "prompt_cache_1h_deep", False)) and mode == "deep",
        getattr(settings, "openrouter_api_key", "") or "",
    )


def _run_engine(
    incoming_text: str,
    *,
    mode: str,
    playbook_version: str | None,
    scope: str = "whole",
    original_text: str | None = None,
) -> ReviewResult:
    """Production path: classify the doc, select the per-variant v4 playbook + baseline,
    build gateways, and run the engine.

    ``scope="redlines"``: review only the document's tracked changes — ``incoming_text`` is the
    REDLINED (changes-accepted) text and ``original_text`` the ORIGINAL (changes-rejected) text, so
    the engine diffs the doc against its OWN original (not the standard template) and grades each
    change against the playbook. Coverage is skipped (only the changes matter).

    Selection precedence: an explicit settings override (engine_playbook_path /
    engine_standard_template_path) ALWAYS wins (pinning / tests). Otherwise the T0 router
    classifies the document ONCE; ``select_variant_playbook`` picks the matching v4 playbook
    + baseline (fail-safe to the protective Company-mutual variant), and that same verdict is
    reused inside ``run_review`` (no second router call). Falls back to the legacy v3 playbook
    + mutual baseline if the v4 manifest is unavailable or routing fails.

    Tests monkeypatch this to avoid live calls.
    """
    from app.settings_store import effective

    cfg = effective()
    gws = build_engine_gateways(cfg, mode=mode)
    quick = mode == "quick"

    pb_override = getattr(settings, "engine_playbook_path", "") or ""
    std_override = getattr(settings, "engine_standard_template_path", "") or ""
    router_obj: dict | None = None
    variant_key: str | None = None
    router_cost = 0.0
    router_degraded = (
        False  # (#8) True if the pre-run router fell back (provider failure)
    )
    router_in = router_out = (
        0  # tokens used by the pre-run router (folded into the review)
    )

    if pb_override or std_override:  # explicit pin wins
        pb_path = pb_override or str(_DEFAULT_PLAYBOOK)
        std_path = std_override or str(_DEFAULT_STANDARD)
    else:
        chosen = None
        gw_router = gws.get("router")
        if gw_router is not None:
            try:
                # Request-scoped ledger (not a shared-counter delta): the router gateway
                # is lru-cached and shared across concurrent reviews, so a counter delta
                # here would absorb another request's router call.
                with track_usage() as _router_ledger:
                    router_obj, router_cost, router_degraded = run_router(
                        gw_router, incoming_text, "v4-route"
                    )
                router_in = _router_ledger.input_tokens
                router_out = _router_ledger.output_tokens
                chosen = select_variant_playbook(router_obj)
            except Exception:  # noqa: BLE001 — never fail the review on routing
                log.exception(
                    "variant routing failed; using the legacy default playbook"
                )
                router_obj, chosen, router_cost, router_in, router_out = (
                    None,
                    None,
                    0.0,
                    0,
                    0,
                )
                router_degraded = False
        if chosen:
            pb_path = str(_REPO / chosen["playbook"])
            std_path = str(_REPO / chosen["baseline"])
            variant_key = chosen.get("variant_key")
        else:
            pb_path = str(_DEFAULT_PLAYBOOK)
            std_path = str(_DEFAULT_STANDARD)

    # Both the playbook and the standard baseline must exist — surface a clean 503, not a
    # raw parse error (the baseline guard is symmetric to the playbook one above).
    if not Path(pb_path).exists():
        raise EngineError(
            503, "playbook_unavailable", f"Playbook not found at {pb_path}"
        )
    if not Path(std_path).exists():
        raise EngineError(
            503, "standard_unavailable", f"Standard template not found at {std_path}"
        )
    try:
        playbook = load_playbook(pb_path)
    except PlaybookValidationError as exc:
        # A structurally invalid playbook (e.g. a typo'd `presence` that would silently drop a
        # required clause from the checklist) surfaces as the same 503 as a missing file.
        raise EngineError(503, "playbook_unavailable", str(exc)) from exc
    # Redlines-only: diff against the doc's OWN original (changes rejected); whole-doc: the standard
    # template. The playbook (selected above via the same router verdict) grounds findings either way.
    redlines = scope == "redlines"
    standard = (original_text or "") if redlines else parse_document(std_path).full_text
    pv = (
        playbook_version
        or variant_key
        or str(playbook.get("variant_key") or playbook.get("version_no") or "v?")
    )

    # Escalate-only embedding signals (S1/S2/S4). ALL are additive and gated on `embeddings_provider`
    # being on (default "off" -> provider is None -> byte-identical engine behavior). Whole scope
    # only: the redlines path diffs a doc against its OWN original, where the playbook index (built
    # against the standard baseline) does not apply. A None provider short-circuits every branch.
    embed_provider = get_provider() if not redlines else None
    clause_match = NO_REPORT
    clause_sim = None
    if embed_provider is not None:
        # S2 report — needs the routed variant + the precomputed index; degrades to NO_REPORT.
        vk = variant_key or str(playbook.get("variant_key") or "")
        clause_match = embed_and_match(incoming_text, vk, embed_provider, load_index())
        clause_sim = make_clause_sim(embed_provider)  # S4

    return run_review(
        gws["primary"],
        incoming_text=incoming_text,
        standard_text=standard,
        playbook=playbook,
        playbook_version=pv,
        # Both tiers are whole-doc-centric: NO per-clause fan-out (it added ~0 recall over the
        # whole-doc pass at most of the cost+FPs — benchmark 2026-06-24). Quick is the whole-doc
        # read alone; deep adds coverage to catch DELETED required clauses (the one thing a whole-doc
        # read can't). Redlines skip coverage too. (A `max` tier with a 2nd Opus read + cross-clause
        # was evaluated and dropped: it was not better than deep at 2.5x the cost.)
        clause_pass=False,
        skip_coverage=(redlines or quick),
        whole_doc=True,
        # Quick is a TRIAGE pass (locate + classify + summarize, no drafting); deep drafts a minimal,
        # surgical redline per finding so the add-in's word-diff stays granular (no whole-para strike).
        wholedoc_style=("triage" if quick else "edit"),
        # Redlines scope: `standard_text` is the counterparty doc's OWN original (changes rejected),
        # so the deep whole-doc pass must fence + relabel it (not the trusted Amperesand template).
        redlines=redlines,
        cross_clause=False,
        self_verify=False,
        # Redlines: original vs redlined are the SAME doc (changes rejected vs accepted), so an
        # unchanged clause is byte-identical (sim 1.0). Use an EXACT unchanged-threshold so a minor
        # but material edit (e.g. a term "1 year" → "10 years") is never discarded as "near-identical".
        unchanged_threshold=1.0 if redlines else None,
        gw_deep=gws.get("deep"),
        gw_router=gws.get("router"),
        router_obj=router_obj,
        # (#8/#2) The router + mode were resolved HERE (pre-run to pick the variant), so pass the
        # pre-run fallback flag and the mode label through for the degradation + integrity blocks.
        router_degraded=router_degraded,
        mode_label=mode,
        # The router ran here (to pick the variant), so fold its cost + tokens into the
        # review's totals — otherwise cost_usd and the token fields would disagree.
        prior_cost=router_cost,
        prior_input_tokens=router_in,
        prior_output_tokens=router_out,
        # Quick runs at LOW effort for speed (lean triage prompt, no standard template); deep gets
        # medium — it's the rich whole-doc minimal-edit pass and is the one we afford the budget.
        effort=("low" if quick else "medium"),
        profile="deep",
        # Escalate-only embedding signals (all no-ops when embeddings are off). S1's deletion
        # pre-check is gated inside run_review to quick + whole scope (deep runs coverage).
        clause_match=clause_match,
        clause_sim=clause_sim,
        embed_provider=(embed_provider if quick else None),
    )


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
_PUBLIC_FINDING_KEYS = (
    "clause_heading",
    "clause_types",
    "change_type",
    "severity",
    "verified_severity",
    "title",
    "rationale",
    "suggested_language",
    "playbook_position",
    "span",
    "span_faithful",
    "confidence",
    "guidance",
    "verify",
)


def _finding_out(f: dict) -> dict:
    return {k: f[k] for k in _PUBLIC_FINDING_KEYS if k in f}


def _redline_plan(findings: list[dict]) -> list[dict]:
    """Provider-neutral edits: find (verbatim span) -> replace + a margin comment.

    Skips findings whose span was checked and found NOT verbatim in the document
    (``span_faithful is False``) — a ``find`` that isn't present would mis-apply. A
    whole-doc finding's unverified span (``None``) is kept (find/replace self-validates:
    no match -> no change), which is why this is looser than the docx path (_redline_issues),
    where an unverified span must not drive a tracked deletion."""
    plan = []
    for f in findings:
        sev = f.get("verified_severity") or f.get("severity")
        if (
            sev in ("high", "medium")
            and f.get("span")
            and f.get("suggested_language")
            and f.get("span_faithful") is not False
        ):
            plan.append(
                {
                    "find": f["span"],
                    "replace": f["suggested_language"],
                    "comment": f"[{sev.upper()}] {f.get('title', '')} — {f.get('rationale', '')}",
                }
            )
    return plan


def _serialize(review_id: str, r: ReviewResult) -> dict:
    return {
        "review_id": review_id,
        "risk_tier": r.risk_tier,
        "adherence_score": r.adherence_score,
        "perspective": r.perspective,
        "playbook_version": getattr(r, "playbook_version", "") or "",
        "routing": r.routing,
        "counts": r.counts,
        "cost_usd": r.cost_usd,
        "input_tokens": getattr(r, "input_tokens", 0),
        "output_tokens": getattr(r, "output_tokens", 0),
        "findings": [_finding_out(f) for f in r.findings],
        "cross_clause_flags": r.cross_clause_flags,
        "mode": getattr(r, "mode", "") or "",
        "coverage": {
            "absent_required": [
                {"item_key": c.item_key, "clause_type": c.clause_type, "note": c.note}
                for c in r.coverage.absent_required
            ],
            # (#2/#8) Did a coverage (deleted-clause) pass actually run, and did it degrade? Quick
            # skips coverage entirely (ran=False); a provider fallback sets degraded=True.
            "ran": getattr(r, "coverage_ran", False),
            "degraded": getattr(r.coverage, "degraded", False),
        },
        # (#2/#8) Analysis integrity: how much to trust this result. `confidence` is "triage" for a
        # quick read (locate+classify, no deleted-clause coverage) vs "full" for deep; `degraded_
        # components` names any part that ran on a provider fallback (tier may be conservative).
        "analysis_integrity": {
            "mode": getattr(r, "mode", "") or "",
            "confidence": "triage" if getattr(r, "mode", "") == "quick" else "full",
            "coverage_ran": getattr(r, "coverage_ran", False),
            "degraded_components": getattr(r, "degraded_components", []) or [],
        },
        # (#6) Provenance: models / provider / prompt + playbook release ids that produced this review.
        "provenance": getattr(r, "provenance", {}) or {},
        # S1 (escalate-only, ADDITIVE): the embedding deletion pre-check's candidates. Kept SEPARATE
        # from coverage.absent_required (deep's authoritative net, which the add-in renders as fact) —
        # these are advisory "maybe missing, run a deep review" signals, empty when embeddings are off.
        "embed_precheck": {"candidates_absent": getattr(r, "embed_precheck", []) or []},
        "redline_plan": _redline_plan(r.findings),
    }


def _extract_text(filename: str | None, data: bytes) -> str:
    suffix = (Path(filename or "doc.txt").suffix or ".txt").lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise EngineError(
            415,
            "unsupported_media_type",
            f"Unsupported file type {suffix!r}; allowed: {sorted(_ALLOWED_SUFFIXES)}.",
        )
    _guard_zip_bomb(data)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tf:
        tf.write(data)
        tf.flush()
        try:
            return parse_document(tf.name).full_text
        except Exception as e:  # noqa: BLE001 — log details server-side, return a generic message
            log.exception("text extraction failed for %s", filename)
            raise EngineError(
                422,
                "unprocessable",
                "Could not extract text from the uploaded document.",
            ) from e


def _extract_redline_pair(filename: str | None, data: bytes) -> tuple[str, str]:
    """``(original_text, redlined_text)`` for a tracked-changes .docx (``scope="redlines"``).

    Reuses the zip-bomb guard; returns a clean 422 ``no_redlines`` when the document carries no
    tracked changes, and maps an unprocessable docx to the same 422 as ``_extract_text``."""
    from app.ingestion.redline_extract import (
        extract_redline_versions,
        has_tracked_changes,
    )

    _guard_zip_bomb(data)
    if not has_tracked_changes(data):
        raise EngineError(
            422,
            "no_redlines",
            "This document has no tracked changes to review. Switch to Whole document.",
        )
    try:
        return extract_redline_versions(data)
    except Exception as e:  # noqa: BLE001 — generic message, details server-side
        log.exception("redline extraction failed for %s", filename)
        raise EngineError(
            422,
            "unprocessable",
            "Could not extract tracked changes from the uploaded document.",
        ) from e


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.post(
    "/reviews",
    response_model=ReviewResponse,
    status_code=201,
    responses={
        200: {
            "model": ReviewResponse,
            "description": "Cached review (idempotent resubmit).",
        }
    },
)
async def create_review(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form("deep"),
    scope: str = Form("whole"),
    playbook_version: str | None = Form(None),
    source_channel: str = Form("api"),
    force: bool = Form(False),
    async_mode: bool = Query(
        False,
        alias="async",
        description="Opt-in async: 202 + job_id instead of holding the connection; "
        "poll GET /v1/reviews/jobs/{job_id}. Sync stays the default.",
    ),
    principal: ResolvedPrincipal = Depends(engine_principal),
) -> JSONResponse:
    # The spine of the system. Flow of this handler (each step is a block below):
    #   validate mode/scope/channel → entitlement gate → extract text
    #   (parse/redline) → dedup (exact-sha + sim-cache) → cost-cap/rate gates → _run_engine
    #   (router picks playbook → run_review) → serialize → persist (best-effort) → 201 (or 200 cache hit).
    if mode not in ("quick", "deep"):
        raise EngineError(400, "bad_request", "mode must be 'quick' or 'deep'.")
    if scope not in ("whole", "redlines"):
        raise EngineError(400, "bad_request", "scope must be 'whole' or 'redlines'.")
    # Redlines review reconstructs original-vs-accepted text from OOXML tracked changes — .docx only.
    if (
        scope == "redlines"
        and (Path(file.filename or "").suffix or "").lower() != ".docx"
    ):
        raise EngineError(
            400,
            "bad_request",
            "scope='redlines' requires a .docx with tracked changes.",
        )
    # P1-1 HARD GATE: the resolved principal must be ENTITLED to this action (quick/deep). A valid
    # key/session that is not entitled is rejected here — the blocker the per-principal allow-list
    # used to leave advisory.
    require_engine_action(principal, mode)
    channel = source_channel if source_channel in _ALLOWED_CHANNELS else "api"
    max_bytes = settings.max_upload_mb * 1024 * 1024
    data = await file.read(max_bytes + 1)  # bounded — don't buffer an oversized body
    if not data:
        raise EngineError(400, "bad_request", "Empty upload.")
    if len(data) > max_bytes:
        raise EngineError(
            413, "request_too_large", f"Upload exceeds {settings.max_upload_mb} MB."
        )
    # Scope-folded dedup key: a whole-doc and a redlines review of the SAME bytes are different
    # analyses and must NOT collide in the cache. Prefix only the redlines preimage so existing
    # whole-doc cache entries keep hitting (no cold cache, no DB migration).
    sha = hashlib.sha256(data if scope == "whole" else b"redlines\0" + data).hexdigest()

    # Explicit idempotency key (X-Idempotency-Key, optional): the caller's per-flow-step uuid.
    # Checked AHEAD of the content-sha tiers because a retried flow-step may re-upload
    # byte-DIFFERENT content (a re-exported docx) that must still replay the first result.
    # Scoped (principal, mode+scope, key) — svc:n8n is one shared principal across flows, and
    # folding mode/scope into the stored key means a deep request can never be served a
    # quick-mode replay recorded under the same caller key. force=true bypasses, mirroring
    # the content tiers.
    _raw_idem = (request.headers.get("x-idempotency-key") or "").strip()
    idem_key = f"{mode}:{scope}:{_raw_idem}"[:128] if _raw_idem else ""
    if idem_key and not force:
        replay = await run_in_threadpool(
            reviews_repo.find_review_by_idempotency_key,
            principal.principal_id,
            idem_key,
            principal.org_id,
        )
        if replay is not None:
            return JSONResponse(replay, status_code=200)

    # Idempotency: identical (document, mode) already reviewed -> return it, no re-charge
    # (covers channel retries on timeout). force=true re-runs (e.g. after a playbook change).
    if not force:
        prior = await run_in_threadpool(
            reviews_repo.find_existing_review, sha, mode, principal.org_id
        )
        if prior is not None:
            # Same shape as reviews_repo._annotate_cache (the normalized-text path) so the
            # `cache` block is uniform across cache tiers. filename/at aren't on the payload.
            prior.setdefault(
                "cache",
                {
                    "hit": True,
                    "tier": "exact",
                    "similarity": 1.0,
                    "matched_review_id": prior.get("review_id", ""),
                    "matched_filename": "",
                    "matched_at": None,
                },
            )
            try:  # audit the exact-sha hit (parity with the norm-text tier)
                await run_in_threadpool(
                    partial(
                        reviews_repo.record_event,
                        review_id=prior.get("review_id", ""),
                        event_type="cache_hit",
                        detail=f"exact match for {file.filename or ''} via {channel}",
                        org_id=principal.org_id,
                    )
                )
            except (
                Exception
            ):  # best-effort audit; never fail the client over a logging write
                log.exception("record_event failed for exact cache hit")
            return JSONResponse(prior, status_code=200)

    # Redlines scope reconstructs (original, redlined) from the .docx tracked changes; the redlined
    # text is the "incoming" reviewed against the original. Whole scope extracts plain text as before.
    if scope == "redlines":
        original_text, text = await run_in_threadpool(
            _extract_redline_pair, file.filename, data
        )
    else:
        original_text = None
        text = await run_in_threadpool(_extract_text, file.filename, data)
    if not text.strip():
        raise EngineError(
            422, "needs_ocr", "No extractable text (document may be scanned)."
        )

    # Cross-channel cache: the same NDA via a different channel extracts to different
    # bytes -> the sha check above misses. Match on the document's NORMALIZED TEXT
    # instead (identical content after whitespace/case/punctuation/unicode canonicalization)
    # and serve the stored review, no LLM spend. Keyed on content, not source_channel.
    # Only an identical-text re-submission hits — a "similar" (edited) NDA gets a fresh
    # review, never a reused one (a one-word legal change must not be served a stale answer).
    norm_sha = simcache.norm_sha256(
        ("redlines\n" + text) if scope == "redlines" else text
    )
    if not force and norm_sha:
        similar = await run_in_threadpool(
            reviews_repo.find_similar_review, norm_sha, mode, principal.org_id
        )
        if similar is not None:
            try:
                await run_in_threadpool(
                    partial(
                        reviews_repo.record_event,
                        review_id=similar["cache"]["matched_review_id"],
                        event_type="cache_hit",
                        detail=f"{similar['cache']['tier']} match for {file.filename or ''} via {channel}",
                        org_id=principal.org_id,
                    )
                )
            except (
                Exception
            ):  # best-effort audit; never fail the client over a logging write
                log.exception("record_event failed for cache hit")
            return JSONResponse(similar, status_code=200)

    # Monthly cost cap: gate BEFORE the paid engine call. The cache tiers above cost nothing and are
    # deliberately never blocked — only a fresh, billable run is capped. The service-account KEY's own
    # ``monthly_cost_cap_usd`` (P1-4) wins; else the global ``engine_monthly_cost_cap_usd`` (P0-12).
    # SOFT guard: a pre-flight read of already-persisted spend (summed from EngineReview by this
    # principal), so a concurrent burst can overshoot it (the atomic counter is a 'Later' item).
    cost_cap = float(
        principal.monthly_cost_cap_usd
        or getattr(settings, "engine_monthly_cost_cap_usd", 0.0)
        or 0.0
    )
    if cost_cap > 0:
        spent = await run_in_threadpool(
            reviews_repo.monthly_cost_usd, principal.principal_id
        )
        if spent >= cost_cap:
            raise EngineError(
                429,
                "cost_cap_exceeded",
                f"Monthly engine spend cap (${cost_cap:.2f}) reached for this key.",
                {"spent_usd": round(spent, 4), "cap_usd": cost_cap},
            )

    # Async opt-in (3.1): every gate above already passed (auth, entitlement, cache tiers
    # missed, cost cap) and the text is EXTRACTED — persist the job and return
    # 202 immediately. The worker's claimer runs the engine; the caller polls the job.
    if async_mode:
        job_id = await run_in_threadpool(
            partial(
                reviews_repo.create_review_job,
                org_id=principal.org_id,
                principal_id=principal.principal_id,
                mode=mode,
                scope=scope,
                playbook_version=playbook_version,
                source_channel=channel,
                doc_filename=file.filename or "",
                doc_sha256=sha,
                norm_sha256=norm_sha,
                incoming_text=text,
                original_text=original_text,
                idempotency_key=idem_key or None,
            )
        )
        return JSONResponse(
            {"job_id": job_id, "status": "pending"},
            status_code=202,
            headers={"Location": f"/v1/reviews/jobs/{job_id}"},
        )

    # Concurrency ceiling: try-acquire a slot ONLY for the paid engine run (the cache/extract
    # paths above are never gated). At capacity this is a typed 429 the caller can back off on —
    # not a threadpool pile-up that exhausts the DB pool and flips /healthz.
    sem = acquire_review_slot()
    # Heavy/blocking work (parse + multi-call LLM engine + DB write) runs off the event loop.
    try:
        result = await run_in_threadpool(
            partial(
                _run_engine,
                text,
                mode=mode,
                playbook_version=playbook_version,
                scope=scope,
                original_text=original_text,
            )
        )
    except EngineError:
        raise  # already a clean, typed error envelope
    except Exception as e:  # noqa: BLE001 — a provider/engine failure must surface as an HONEST
        # error, never a silent clean review: the whole-doc pass is the sole finding source, so a
        # failure there used to be swallowed into a green/100%-adherence result for an unread document.
        log.exception("review engine failed (mode=%s, scope=%s)", mode, scope)
        raise EngineError(
            503,
            "review_failed",
            "The document could not be analyzed (the review engine failed). Please retry.",
        ) from e
    finally:
        sem.release()
    review_id = uuid.uuid4().hex
    out = _serialize(review_id, result)
    try:
        await run_in_threadpool(
            partial(
                reviews_repo.save_review,
                out,
                mode=mode,
                source_channel=channel,
                doc_filename=file.filename or "",
                doc_sha256=sha,
                norm_sha256=norm_sha,
                org_id=principal.org_id,
                actor_user_id=principal.principal_id,
            )
        )
        if idem_key:
            # Map the caller's key to this run (first writer wins). Inside the same try:
            # if the review itself failed to persist there is nothing to replay against.
            await run_in_threadpool(
                reviews_repo.record_review_idempotency_key,
                principal.principal_id,
                idem_key,
                review_id,
                principal.org_id,
            )
    except Exception:  # noqa: BLE001 — persistence is best-effort; never lose a paid result
        log.exception("save_review failed for review %s", review_id)
    return JSONResponse(out, status_code=201)


@router.get("/reviews")
async def list_reviews(
    limit: int = 50, offset: int = 0, _p: ResolvedPrincipal = Depends(engine_principal)
) -> JSONResponse:
    """Review history (newest first) for the caller's org, for audit + the channels' history views."""
    rows = await run_in_threadpool(
        partial(reviews_repo.list_reviews, org_id=_p.org_id, limit=limit, offset=offset)
    )
    return JSONResponse({"reviews": rows})


@router.get("/reviews/jobs/{job_id}")
async def get_review_job(
    job_id: str, _p: ResolvedPrincipal = Depends(engine_principal)
) -> JSONResponse:
    """Poll an async review job (3.1). ``status`` walks pending -> running -> done|failed;
    when done the completed review payload is inlined so n8n needs no second call. Org-scoped
    like every review read (a foreign org's job id is a 404, not a leak)."""
    job = await run_in_threadpool(reviews_repo.get_review_job, job_id, _p.org_id)
    if job is None:
        raise EngineError(404, "not_found", f"No review job {job_id!r}.")
    if job.get("status") == "done" and job.get("review_id"):
        job["review"] = await run_in_threadpool(
            reviews_repo.get_review, job["review_id"], _p.org_id
        )
    return JSONResponse(job)


@router.get("/reviews/{review_id}")
async def get_review(
    review_id: str, _p: ResolvedPrincipal = Depends(engine_principal)
) -> JSONResponse:
    out = await run_in_threadpool(reviews_repo.get_review, review_id, _p.org_id)
    if out is None:
        raise EngineError(404, "not_found", f"No review {review_id!r}.")
    return JSONResponse(out)


@router.post("/redline", response_model=RedlineResponse)
async def redline(
    review_id: str = Form(...),
    principal: ResolvedPrincipal = Depends(engine_principal),
) -> JSONResponse:
    # Shape guard parity with redline_docx: review ids are uuid4 hex. Reject anything else before
    # the org-scoped lookup (a foreign/garbage id already 404s, but state the invariant explicitly).
    if not _REVIEW_ID_RE.fullmatch(review_id):
        raise EngineError(404, "not_found", f"No review {review_id!r}.")
    # P1-1 HARD GATE: producing a redline is the 'redline' engine action — the principal must be
    # entitled (a quick-only key/allow-list entry is rejected here).
    try:
        require_engine_entitlement(principal, "redline")
    except NotEntitled as e:
        raise EngineError(403, "not_entitled", e.message, e.details) from e
    # Tenant scope: redline only a review in the caller's org (a cross-org id resolves to None -> 404).
    out = await run_in_threadpool(reviews_repo.get_review, review_id, principal.org_id)
    if out is None:
        raise EngineError(404, "not_found", f"No review {review_id!r}.")
    try:
        await run_in_threadpool(
            partial(
                reviews_repo.record_event,
                review_id=review_id,
                event_type="redline",
                detail=f"{len(out.get('redline_plan', []))} edits",
                org_id=principal.org_id,
            )
        )
    except Exception:  # best-effort audit; never fail the client over a logging write
        log.exception("record_event failed for review %s", review_id)
    return JSONResponse(
        {"review_id": review_id, "redline_plan": out.get("redline_plan", [])}
    )


def _redline_issues(payload: dict) -> list[SimpleNamespace]:
    """Map a stored review payload's findings to issue-like objects for the OOXML
    writer. A finding becomes an *accepted* tracked change (delete span -> insert
    suggestion) only when its span was VERIFIED verbatim in the document
    (``span_faithful is True``) plus a suggestion and high/medium severity. This is
    STRICTER than ``redline_plan``: whole-doc-pass findings carry an unverified
    (``span_faithful is None``) model-generated span, so they must NOT drive a tracked
    deletion of text that may not appear in the document — they render as a plain
    clause + reviewer note instead. ``build_redlined_docx`` reads these attributes off
    any object, so SimpleNamespace stand-ins are fine."""
    issues = []
    for f in payload.get("findings", []):
        sev = f.get("verified_severity") or f.get("severity") or "low"
        span = f.get("span") or ""
        suggestion = f.get("suggested_language") or ""
        applyable = bool(
            span
            and suggestion
            and f.get("span_faithful") is True
            and sev in ("high", "medium")
        )
        # Surface the UNVERIFIED/unfaithful-span caveat in the docx note. Gate STRICTLY on
        # ``span_faithful is False`` — the authoritative "cited span not found verbatim" signal: the
        # producers set guidance to the warning text exactly on that path (walkaway._finding sets the
        # UNVERIFIED note only when faithful is False; run_finding folds "UNFAITHFUL SPAN: …" into
        # guidance only when the span fails check_span). Do NOT trigger on mere presence of guidance —
        # the per-clause model is prompted to fill ``guidance`` with normal negotiation notes on
        # FAITHFUL findings too, and decorating a verified, applied tracked change with the ⚠
        # "confirm manually" glyph would mislabel it as unverified.
        rationale = f.get("rationale") or ""
        if f.get("span_faithful") is False:
            caveat = f.get("guidance") or (
                "UNVERIFIED: the cited span was not found verbatim in the document — confirm manually."
            )
            rationale = f"⚠ {caveat}" + (f" — {rationale}" if rationale else "")
        issues.append(
            SimpleNamespace(
                clause_number="",
                clause_heading=f.get("clause_heading") or "",
                incoming_text=span,
                suggested_language=suggestion,
                status="accepted" if applyable else "",
                severity=sev,
                title=f.get("title") or "",
                rationale=rationale,
            )
        )
    return issues


@router.get("/reviews/{review_id}/redline.docx")
async def redline_docx(
    review_id: str, _p: ResolvedPrincipal = Depends(engine_principal)
) -> FileResponse:
    """G1 — the on-request deep+docx path: a tracked-changes clause-summary .docx built
    from the stored review. Reuses the canonical OOXML writer (the same redline engine
    the web app exports). The .docx is rebuilt on every request (the stored review is
    immutable, so the rebuild is deterministic)."""
    # Explicit identity guard: review ids are uuid4 hex. Reject anything else before
    # building a path, so the no-traversal property is stated, not just incidental to
    # the route not matching '/' + the get_review-None 404 below.
    if not _REVIEW_ID_RE.fullmatch(review_id):
        raise EngineError(404, "not_found", f"No review {review_id!r}.")
    payload = await run_in_threadpool(
        reviews_repo.get_review, review_id, _p.org_id
    )  # org-scoped
    if payload is None:
        raise EngineError(404, "not_found", f"No review {review_id!r}.")

    out_path = settings.exports_path / f"{review_id}_v1_redline.docx"
    tmp_path = settings.exports_path / f"{review_id}_v1_redline.{uuid.uuid4().hex}.tmp"
    counterparty = ((payload.get("routing") or {}).get("router", {}) or {}).get(
        "counterparty_name", ""
    )
    title = _safe_filename_title(counterparty) or f"NDA Review {review_id[:8]}"
    review_like = SimpleNamespace(title=title, provider="", model="")
    issues = _redline_issues(payload)

    def _build_and_publish() -> None:
        # Build to a UNIQUE temp file then atomically replace, so a concurrent download of the same
        # review can never stream a half-written file (the rebuild is deterministic — any complete
        # version is correct). The temp is removed if the replace didn't consume it (build failure).
        try:
            build_redlined_docx(review_like, issues, tmp_path)
            tmp_path.replace(out_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    await run_in_threadpool(_build_and_publish)
    try:
        tracked = sum(
            1 for i in issues if i.status == "accepted"
        )  # actual tracked-change count
        await run_in_threadpool(
            reviews_repo.record_event,
            review_id=review_id,
            event_type="redline_docx",
            detail=f"{tracked} tracked changes",
            org_id=_p.org_id,
        )
    except Exception:  # best-effort audit; never fail the download over a logging write
        log.exception("record_event failed for redline.docx %s", review_id)
    return FileResponse(
        str(out_path),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"{title}_redline.docx",
    )


def register(app: FastAPI) -> None:
    """Mount the v1 router. The EngineError handler is registered once centrally in app.main."""
    app.include_router(router)
