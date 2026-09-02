"""Clause-level embedding alignment (escalate-only substrate).

Given an incoming NDA's text and a routed variant, align each incoming clause against that
variant's precomputed playbook embeddings and report:

  * which incoming clauses are VERBATIM matches of a baseline clause (identity, by normalized-text
    sha — cheap, exact, no embedding needed);
  * the best embedding match for each remaining incoming clause (baseline association + a
    ``clause_type`` attribution from the closest position);
  * incoming clauses that hit a walk-away TRIGGER above ``embed_trigger_threshold`` — the only
    escalating signal this substrate emits.

Nothing here is wired into ``run_review`` yet. Every unavailable path (provider off, index missing
or stale, any failure) returns :data:`NO_REPORT` — an empty, harmless report — matching the
router-failure norm: a broken optional signal must never fail (or alter) the review.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from app.config import settings
from app.engine.embeddings import EmbeddingProvider, PlaybookIndex
from app.engine.simcache import norm_sha256
from app.ingestion.segmenter import segment_clauses

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ClauseMatch:
    """One incoming clause's best baseline association."""

    clause_idx: int
    clause_text: str
    baseline_idx: int
    baseline_text: str
    score: float
    clause_type: str  # attributed from the nearest position; "" when unknown


@dataclass(slots=True)
class ClauseMatchReport:
    """The outcome of aligning an incoming document against a variant's playbook embeddings.

    * ``verbatim`` — incoming clause indices whose normalized text is byte-identical to a baseline
      clause (exact identity; no embedding scrutiny needed).
    * ``matched`` — best embedding association for each non-verbatim incoming clause that clears
      ``embed_cov_threshold``.
    * ``unmatched_incoming`` — incoming clause indices with no baseline match above the floor
      (candidate NEW / non-standard language).
    * ``unmatched_baseline`` — baseline clause indices no incoming clause matched (candidate MISSING
      language).
    * ``trigger_hits`` — ``(clause_idx, trigger_text, score)`` for incoming clauses that resemble a
      walk-away trigger above ``embed_trigger_threshold`` (the escalating signal).
    """

    verbatim: list[int] = field(default_factory=list)
    matched: list[ClauseMatch] = field(default_factory=list)
    unmatched_incoming: list[int] = field(default_factory=list)
    unmatched_baseline: list[int] = field(default_factory=list)
    trigger_hits: list[tuple[int, str, float]] = field(default_factory=list)


# The single empty report handed back whenever alignment is unavailable (never None so callers can
# treat "off" and "no matches" uniformly).
NO_REPORT = ClauseMatchReport()


def embed_and_match(
    incoming_text: str,
    variant_key: str,
    provider: EmbeddingProvider | None,
    index: PlaybookIndex | None,
) -> ClauseMatchReport:
    """Align ``incoming_text`` against ``variant_key``'s embeddings; see :class:`ClauseMatchReport`.

    Returns :data:`NO_REPORT` when the provider or index is unavailable, when the variant has no
    baseline vectors, or on ANY failure (logged, never raised).
    """
    if provider is None or index is None:
        return NO_REPORT
    try:
        clauses = segment_clauses(incoming_text)
        if not clauses:
            return NO_REPORT

        base_vecs, base_meta = index.get(variant_key, "baseline")
        if base_vecs.size == 0:
            return NO_REPORT
        baseline_hashes = index.baseline_hashes(variant_key)
        pos_vecs, pos_meta = index.get(variant_key, "positions")
        trig_vecs, trig_meta = index.get(variant_key, "triggers")

        embedded = provider.embed([c.text for c in clauses])
        if embedded is None or embedded.size == 0 or embedded.shape[0] != len(clauses):
            return NO_REPORT

        report = ClauseMatchReport()
        matched_baseline: set[int] = set()

        # Brute-force cosine: rows are unit vectors, so a matmul IS the cosine matrix.
        base_sims = embedded @ base_vecs.T  # (n_clauses, n_baseline)
        pos_sims = embedded @ pos_vecs.T if pos_vecs.size else None
        trig_sims = embedded @ trig_vecs.T if trig_vecs.size else None

        for i, clause in enumerate(clauses):
            # (a) exact identity via normalized-text sha — verbatim baseline clause.
            sha = norm_sha256(clause.text)
            if sha and sha in baseline_hashes:
                report.verbatim.append(i)

            # (b) best baseline association.
            b = int(np.argmax(base_sims[i]))
            score = float(base_sims[i][b])
            if score >= settings.embed_cov_threshold:
                matched_baseline.add(b)
                # (c) clause_type attribution from the nearest position.
                clause_type = ""
                if pos_sims is not None:
                    p = int(np.argmax(pos_sims[i]))
                    clause_type = (
                        str(pos_meta[p].get("clause_type", "")) if pos_meta else ""
                    )
                report.matched.append(
                    ClauseMatch(
                        clause_idx=i,
                        clause_text=clause.text,
                        baseline_idx=b,
                        baseline_text=str(base_meta[b].get("text", ""))
                        if base_meta
                        else "",
                        score=score,
                        clause_type=clause_type,
                    )
                )
            elif i not in report.verbatim:
                report.unmatched_incoming.append(i)

            # (d) walk-away trigger hits (the escalating signal).
            if trig_sims is not None:
                t = int(np.argmax(trig_sims[i]))
                t_score = float(trig_sims[i][t])
                if t_score >= settings.embed_trigger_threshold:
                    trigger_text = (
                        str(trig_meta[t].get("text", "")) if trig_meta else ""
                    )
                    report.trigger_hits.append((i, trigger_text, t_score))

        report.unmatched_baseline = [
            j for j in range(base_vecs.shape[0]) if j not in matched_baseline
        ]
        return report
    except Exception:  # noqa: BLE001 — alignment is optional; degrade to an empty report
        log.exception(
            "embedding alignment failed for variant %r; returning empty report",
            variant_key,
        )
        return NO_REPORT


# --------------------------------------------------------------------------- #
# S4 — alignment upgrade: an embedding cosine similarity for clause pairing.
# --------------------------------------------------------------------------- #
def make_clause_sim(
    provider: EmbeddingProvider | None,
) -> Callable[[str, str], float] | None:
    """A cosine ``(a, b) -> [0, 1]`` similarity over ``provider`` embeddings, or ``None``.

    Returns ``None`` when the provider is off — the caller then keeps its difflib similarity, so an
    ``off`` provider is byte-identical to the pre-embedding path (the hard invariant). The returned
    function is ADDITIVE: it only ever refines the clause-pairing score used inside ``align_clauses``;
    it never removes or skips a clause. Any embedding failure (``None`` / shape mismatch) degrades to
    the difflib char-ratio for that one pair, so a broken provider can only lose the upgrade, never
    the review. Embeddings are memoized per text so a full alignment embeds each clause once.
    """
    if provider is None:
        return None

    from app.redline.differ import (
        similarity,  # noqa: PLC0415 — avoid a cycle at import time
    )

    cache: dict[str, np.ndarray | None] = {}

    def _vec(text: str) -> np.ndarray | None:
        if text not in cache:
            try:
                out = provider.embed([text])
                cache[text] = out[0] if out is not None and out.shape[0] == 1 else None
            except Exception:  # noqa: BLE001 — a failed embed falls back to difflib
                cache[text] = None
        return cache[text]

    def _sim(a: str, b: str) -> float:
        va, vb = _vec(a), _vec(b)
        if va is None or vb is None:
            return similarity(a, b)  # difflib fallback — never worse than the baseline
        # Rows are L2-normalized, so a dot product IS the cosine. Clamp to [0, 1] (a slightly
        # negative cosine on near-orthogonal vectors would be a nonsensical "similarity").
        return max(0.0, min(1.0, float(va @ vb)))

    return _sim


# --------------------------------------------------------------------------- #
# S1 — quick-tier deletion pre-check (embedding coverage probe).
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class PrecheckCandidate:
    """A required checklist item whose best embedding match in the doc is below the floor."""

    clause_type: str
    title: str
    score: float


def precheck_absent(
    incoming_text: str,
    checklist: list,
    provider: EmbeddingProvider | None,
) -> list[PrecheckCandidate]:
    """Required checklist items with NO incoming clause above ``embed_cov_threshold`` — candidates
    for a possibly-absent required clause (the S1 escalate-only pre-check).

    Returns ``[]`` when the provider is off, the doc has no clauses, or on ANY failure (logged). This
    is a PURELY ADDITIVE advisory: it only ever ADDS a candidate; it never removes a checklist item,
    finding, or LLM call. Each item's query text is its playbook ``required_position`` (falling back
    to the human label) embedded against the incoming clause vectors.
    """
    if provider is None or not checklist:
        return []
    try:
        clauses = segment_clauses(incoming_text)
        if not clauses:
            return []
        doc_vecs = provider.embed([c.text for c in clauses])
        if doc_vecs is None or doc_vecs.size == 0 or doc_vecs.shape[0] != len(clauses):
            return []
        queries = [
            (getattr(it, "required_position", "") or getattr(it, "label", ""))
            for it in checklist
        ]
        q_vecs = provider.embed(queries)
        if q_vecs is None or q_vecs.shape[0] != len(checklist):
            return []
        sims = q_vecs @ doc_vecs.T  # (n_items, n_clauses); rows unit -> cosine
        out: list[PrecheckCandidate] = []
        for i, item in enumerate(checklist):
            best = float(np.max(sims[i])) if sims.shape[1] else 0.0
            if best < settings.embed_cov_threshold:
                out.append(
                    PrecheckCandidate(
                        clause_type=getattr(item, "clause_type", ""),
                        title=getattr(item, "label", "")
                        or getattr(item, "clause_type", ""),
                        score=best,
                    )
                )
        return out
    except Exception:  # noqa: BLE001 — an optional pre-check must never fail the review
        log.exception("embedding deletion pre-check failed; returning no candidates")
        return []
