"""Clause alignment.

Pair clauses of an incoming NDA against the company template so each pair can
be diffed and judged independently. Matching is heuristic and fully
deterministic (no model calls):

1. Match by normalized heading equality / strong heading overlap.
2. Greedily match the remainder by best text similarity above a threshold.
3. Whatever is left over is a structural change:
   - template-only  -> "deletion"  (counterparty dropped the clause)
   - incoming-only  -> "addition"  (counterparty added a clause)
4. Matched pairs whose text is effectively identical are treated as unchanged
   and are NOT returned. Everything else is "modification".

Only *material* pairs (modifications + additions + deletions) are returned, in
incoming-document order where possible.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from app.ingestion.segmenter import Clause
from app.redline.differ import similarity

# A pair below this similarity is too dissimilar to be the "same" clause.
_MATCH_THRESHOLD = 0.45
# At/above this similarity the wording is effectively identical (unchanged).
_UNCHANGED_THRESHOLD = 0.985
# Heading overlap (Jaccard of significant tokens) considered a strong match.
_HEADING_OVERLAP = 0.6
# Cheap word-overlap floor below which we skip the expensive difflib ratio. This
# is a HEURISTIC speedup, not a strict equivalence: difflib runs over the full
# character stream while this floor uses significant word tokens, so in rare
# borderline cases (char-similarity just over the match threshold but almost no
# shared meaningful words) a pair could be reclassified from modification to
# addition+deletion. Realistic clause modifications share many words (or match by
# heading in Pass 1) and are unaffected; the eval harness guards the realistic
# cases. Kept conservative and only applied when both clauses have significant
# tokens. See §2.6.
_JACCARD_FLOOR = 0.1

_WORD_RE = re.compile(r"[a-z0-9]+")

# Positional pseudo-headings from the segmenter's paragraph fallback (e.g.
# "Paragraph 29"). They carry NO semantic signal — two docs both have a
# "Paragraph 29" at different content — so they must NOT be used for heading
# equality matching, or any insertion/deletion cascades into mass false
# "modifications". When a heading is positional we fall back to text similarity.
_POSITIONAL_HEADING = re.compile(r"^paragraph \d+$")

# Boilerplate heading tokens that carry little discriminating signal.
_STOPWORDS = frozenset(
    {
        "of",
        "the",
        "and",
        "or",
        "to",
        "a",
        "an",
        "for",
        "in",
        "on",
        "no",
        "any",
        "this",
        "from",
        "with",
        "by",
        "clause",
        "section",
        "article",
    }
)


@dataclass(slots=True)
class ClausePair:
    """One aligned (or unmatched) clause across the two documents."""

    change_type: str  # "addition" | "deletion" | "modification"
    template: Clause | None
    incoming: Clause | None
    similarity: float = 0.0


# --------------------------------------------------------------------------- #
# Clause field accessors (defensive: the segmenter's Clause may name fields
# slightly differently; we read the common ones and fall back gracefully).
# --------------------------------------------------------------------------- #
def _clause_text(clause: Clause | None) -> str:
    if clause is None:
        return ""
    for attr in ("text", "body", "content"):
        val = getattr(clause, attr, None)
        if isinstance(val, str) and val.strip():
            return val
    val = getattr(clause, "text", "")
    return val if isinstance(val, str) else ""


def _clause_heading(clause: Clause | None) -> str:
    if clause is None:
        return ""
    for attr in ("heading", "title", "name"):
        val = getattr(clause, attr, None)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _normalize_heading(heading: str) -> str:
    """Lower-cased, punctuation/number-stripped heading for equality checks."""
    h = heading.lower()
    h = re.sub(r"^\s*\d+(\.\d+)*[.)]?\s*", "", h)  # drop leading numbering
    h = re.sub(r"[^a-z0-9\s]", " ", h)
    h = re.sub(r"\s+", " ", h).strip()
    return h


def _heading_tokens(heading: str) -> frozenset[str]:
    norm = _normalize_heading(heading)
    return frozenset(t for t in norm.split() if t and t not in _STOPWORDS)


def _heading_overlap(a: str, b: str) -> float:
    """Jaccard similarity of significant heading tokens (0..1)."""
    ta, tb = _heading_tokens(a), _heading_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def _body_tokens(clause: Clause | None) -> frozenset[str]:
    """Significant lowercase word tokens of a clause body (for a cheap overlap)."""
    text = _clause_text(clause).lower()
    return frozenset(t for t in _WORD_RE.findall(text) if t not in _STOPWORDS)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _pair_similarity(
    template: Clause,
    incoming: Clause,
    text_sim_fn: Callable[[str, str], float] = similarity,
) -> float:
    """Combined text + heading similarity used for greedy matching.

    ``text_sim_fn`` is the text-similarity primitive (default the difflib char-ratio). S4 passes an
    embedding cosine here when embeddings are on; it only refines the score, and its own difflib
    fallback keeps the ``off``/failure path byte-identical to the pre-embedding behavior."""
    text_sim = text_sim_fn(_clause_text(template), _clause_text(incoming))
    head_sim = _heading_overlap(_clause_heading(template), _clause_heading(incoming))
    # Text dominates; heading nudges ties between similarly-worded clauses.
    return max(text_sim, 0.85 * text_sim + 0.15 * head_sim)


# --------------------------------------------------------------------------- #
# Alignment
# --------------------------------------------------------------------------- #
def align_clauses(
    template_clauses: list[Clause],
    incoming_clauses: list[Clause],
    *,
    unchanged_threshold: float | None = None,
    text_sim_fn: Callable[[str, str], float] | None = None,
) -> list[ClausePair]:
    """Align template vs. incoming clauses; return only material pairs.

    Returned pairs preserve incoming-document order; template-only deletions are
    appended afterwards in template order.

    ``unchanged_threshold`` is the similarity at/above which a matched pair is treated as
    "unchanged" and dropped (default ``_UNCHANGED_THRESHOLD`` = 0.985, a fuzzy guard against
    near-identical clauses vs a *standard template*). The redlines-only scope passes ``1.0`` so a
    matched pair is dropped ONLY when byte-identical — because there the two sides are the SAME
    document with changes rejected vs accepted, so an unchanged clause is exactly identical while a
    minor edit (e.g. a term "1 year" → "10 years") is < 1.0 and MUST be reviewed.

    ``text_sim_fn`` (S4) overrides the text-similarity primitive used for PAIRING only — an
    embedding cosine when embeddings are on. ``None`` (the default / provider off) uses the difflib
    char-ratio, so the pairing is byte-identical to the pre-embedding behavior. It only refines HOW
    clauses pair; it never adds, removes, or drops a clause from the returned pairs. In particular
    the unchanged-DROP decision (``sim >= unchanged_threshold`` -> pair not returned) is ALWAYS made
    on the difflib ratio, never on ``text_sim_fn``: an embedding must only ever escalate review
    work, so it may not let a reworded clause clear the drop bar where difflib would not.
    """
    sim_fn = text_sim_fn or similarity
    thr = _UNCHANGED_THRESHOLD if unchanged_threshold is None else unchanged_threshold
    n_t = len(template_clauses)
    n_i = len(incoming_clauses)

    matched_template: set[int] = set()
    matched_incoming: set[int] = set()
    # incoming index -> (template index, similarity)
    match_for_incoming: dict[int, tuple[int, float]] = {}

    # ---- Pass 1: strong heading match -----------------------------------
    for ti, t_clause in enumerate(template_clauses):
        t_head = _clause_heading(t_clause)
        t_norm = _normalize_heading(t_head)
        if not t_norm or _POSITIONAL_HEADING.match(t_norm):
            continue
        best_ii = -1
        best_overlap = 0.0
        for ii, i_clause in enumerate(incoming_clauses):
            if ii in matched_incoming:
                continue
            i_head = _clause_heading(i_clause)
            i_norm = _normalize_heading(i_head)
            if not i_norm or _POSITIONAL_HEADING.match(i_norm):
                continue
            overlap = 1.0 if t_norm == i_norm else _heading_overlap(t_head, i_head)
            if overlap > best_overlap:
                best_overlap = overlap
                best_ii = ii
        if best_ii >= 0 and best_overlap >= _HEADING_OVERLAP:
            matched_template.add(ti)
            matched_incoming.add(best_ii)
            sim = sim_fn(
                _clause_text(t_clause), _clause_text(incoming_clauses[best_ii])
            )
            match_for_incoming[best_ii] = (ti, sim)

    # ---- Pass 2: greedy best-similarity match on the remainder ----------
    # Precompute body-token sets once (O(n+m)) for the cheap pre-filter, so the
    # expensive O(n*m) difflib ratio only runs on plausibly-related pairs.
    t_tokens = [_body_tokens(c) for c in template_clauses]
    i_tokens = [_body_tokens(c) for c in incoming_clauses]

    candidates: list[tuple[float, int, int]] = []
    for ti in range(n_t):
        if ti in matched_template:
            continue
        for ii in range(n_i):
            if ii in matched_incoming:
                continue
            # Skip the difflib ratio for pairs with negligible word overlap (very
            # unlikely to clear _MATCH_THRESHOLD — heuristic, see _JACCARD_FLOOR).
            # Only prune when BOTH clauses have significant tokens, so
            # degenerate/empty bodies still fall through to the full similarity
            # (which also weighs the heading).
            ta, tb = t_tokens[ti], i_tokens[ii]
            if ta and tb and _jaccard(ta, tb) < _JACCARD_FLOOR:
                continue
            sim = _pair_similarity(template_clauses[ti], incoming_clauses[ii], sim_fn)
            if sim >= _MATCH_THRESHOLD:
                candidates.append((sim, ti, ii))

    candidates.sort(key=lambda c: c[0], reverse=True)
    for sim, ti, ii in candidates:
        if ti in matched_template or ii in matched_incoming:
            continue
        matched_template.add(ti)
        matched_incoming.add(ii)
        match_for_incoming[ii] = (ti, sim)

    # ---- Emit material pairs in incoming order --------------------------
    # ``sim`` (from ``sim_fn``) decides HOW clauses paired above and may be an embedding cosine.
    # The unchanged-DROP decision, however, must always use the difflib char-ratio: a reworded but
    # semantically-close clause can clear the 0.985 cosine bar where difflib would not, which would
    # DROP a pair the difflib path would have reviewed — an embedding-caused REDUCTION of review
    # work that violates the escalate-only invariant. Recompute the drop score on difflib so
    # enabling embeddings can never remove a pair the ``off`` path keeps.
    pairs: list[ClausePair] = []
    for ii, i_clause in enumerate(incoming_clauses):
        if ii in match_for_incoming:
            ti, sim = match_for_incoming[ii]
            drop_sim = _pair_similarity(template_clauses[ti], i_clause, similarity)
            if drop_sim >= thr:
                continue  # effectively identical (by difflib) -> not material
            pairs.append(
                ClausePair(
                    change_type="modification",
                    template=template_clauses[ti],
                    incoming=i_clause,
                    similarity=sim,
                )
            )
        else:
            pairs.append(
                ClausePair(
                    change_type="addition",
                    template=None,
                    incoming=i_clause,
                    similarity=0.0,
                )
            )

    # Template-only clauses -> deletions, appended in template order.
    for ti, t_clause in enumerate(template_clauses):
        if ti not in matched_template:
            pairs.append(
                ClausePair(
                    change_type="deletion",
                    template=t_clause,
                    incoming=None,
                    similarity=0.0,
                )
            )

    return pairs
