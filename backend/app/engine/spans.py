"""Span faithfulness (improvement B) — near-free, deterministic.

After a model emits a finding with a cited ``span`` (a verbatim quote) and
optionally a character offset, confirm the quote actually occurs in the document
at (about) that offset. A quote that isn't in the document is a hallucinated
citation and must be flagged/rejected. This upgrades provenance (P1-5) from
"a trace exists" to "the trace is faithful" — for the cost of a string match.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

#: Zero-width / invisible format characters that must NOT count as a difference between a cited
#: quote and the document. Critically this includes U+200B: ``gateway.fence_document`` injects a
#: U+200B after the ``<`` of any literal ``</document>`` in the doc to neutralize prompt injection,
#: and the model is shown that fenced text — so it faithfully quotes a span containing the ZWSP. The
#: faithfulness check runs against the RAW (un-fenced) document, and Python's ``re`` ``\s`` does NOT
#: match U+200B, so without stripping it a genuinely-present clause is wrongly judged unfaithful
#: (false "missing required clause" -> forced-RED tier; false "UNVERIFIED" walk-away).
_ZW = re.compile(
    "[\u200b\u200c\u200d\u2060\ufeff\u00ad]"
)  # ZWSP, ZWNJ, ZWJ, WORD JOINER, BOM, SOFT HYPHEN


def _strip_zw(s: str) -> str:
    return _ZW.sub("", s)


#: Smart punctuation folded to ASCII so it doesn't count as a difference vs the document. The Word
#: add-in folds the SAME characters when it locates a clause, so the backend's faithfulness verdict
#: lines up with what the add-in can actually find (curly quotes/dashes were previously judged
#: unfaithful here even though the add-in would have located them).
_UNIFY = {ord(c): "'" for c in "‘’‚‛′`"}
_UNIFY.update({ord(c): '"' for c in "“”„‟″"})
_UNIFY.update({ord(c): "-" for c in "–—−"})
#: Zero-width / format chars to drop entirely (kept in sync with ``_ZW``) for the index-mapped norm.
_ZW_SET = frozenset("\u200b\u200c\u200d\u2060\ufeff\u00ad")  # ZWSP ZWNJ ZWJ WJ BOM SHY


def _unify(s: str) -> str:
    return s.translate(_UNIFY)


def _norm(s: str) -> str:
    """Whitespace-collapsed, case-folded, zero-width-stripped, smart-quote/dash-folded — a tolerant
    existence check that matches what the Word add-in can locate. Delegates to :func:`_norm_with_map`
    so the two normalizations can NEVER diverge (notably per-char vs whole-string ``str.lower()``,
    which differ on the Greek final sigma)."""
    return _norm_with_map(s)[0]


def normalize_text(s: str) -> str:
    """Public alias for :func:`_norm` — the tolerant normalization a caller can compute ONCE for a
    document and pass into :func:`check_span` (``norm_doc=``) so a loop over many checklist items /
    findings doesn't re-walk the whole document per call."""
    return _norm(s)


def _norm_with_map(raw: str) -> tuple[str, list[int]]:
    """Normalize like :func:`_norm` but also return ``idx[j]`` = the raw index of the j-th
    normalized character, so a normalized match can recover the EXACT raw substring of ``raw``.
    Produces the same normalized string :func:`_norm` would for the same input."""
    out: list[str] = []
    idx: list[int] = []
    prev_space = True
    for i, ch in enumerate(raw):
        if ch in _ZW_SET:
            continue
        u = _unify(ch)
        if u.isspace():  # collapses runs (incl. NBSP) to a single space
            if not prev_space:
                out.append(" ")
                idx.append(i)
                prev_space = True
        else:
            # str.lower() is 1→many for some codepoints (e.g. İ U+0130 → "i" + U+0307), so record
            # one raw index PER output char — otherwise idx desyncs from the string it indexes and a
            # later match recovers the wrong raw slice (or overruns).
            lo = u.lower()
            out.append(lo)
            idx.extend([i] * len(lo))
            prev_space = False
    if out and out[-1] == " ":  # drop a trailing space
        out.pop()
        idx.pop()
    return "".join(out), idx


def _occurrences(doc: str, q: str) -> list[int]:
    out: list[int] = []
    i = doc.find(q)
    while i != -1:
        out.append(i)
        i = doc.find(q, i + 1)
    return out


@dataclass(frozen=True)
class SpanCheck:
    """Result of verifying a cited quote against the document.

    ``faithful`` — the quote occurs in the document (exact or whitespace/case
    normalized). ``offset_verified`` — only meaningful when a ``claimed_offset``
    was given: ``True``/``False`` if an occurrence is/ isn't within tolerance of
    it, ``None`` if no offset was claimed or it could not be checked.
    """

    faithful: bool
    found_offset: int | None
    offset_verified: bool | None
    note: str


def check_span(
    doc_text: str,
    quote: str,
    claimed_offset: int | None = None,
    *,
    offset_tolerance: int = 3,
    norm_doc: str | None = None,
) -> SpanCheck:
    """Verify ``quote`` exists in ``doc_text`` (and, if given, near ``claimed_offset``).

    ``norm_doc`` may pass a PRE-computed :func:`normalize_text` of ``doc_text`` so a caller that
    checks many quotes against the same document (e.g. the coverage checklist loop) normalizes the
    document once instead of per call. When ``None`` it is computed here (single-call path)."""
    # Strip zero-width/format chars (notably fence_document's injected U+200B) so a faithfully-quoted
    # span still matches the raw document on the EXACT path and keeps its offset — not just the
    # normalized fallback. doc_text is the raw (un-fenced) document, which never carries the ZWSP.
    q = _strip_zw((quote or "").strip())
    if not q:
        return SpanCheck(False, None, None, "empty quote")

    occ = _occurrences(doc_text, q)
    if occ:
        if claimed_offset is None:
            return SpanCheck(True, occ[0], None, "exact match")
        nearest = min(occ, key=lambda o: abs(o - claimed_offset))
        ok = abs(nearest - claimed_offset) <= offset_tolerance
        note = "exact match; offset " + (
            "verified"
            if ok
            else f"mismatch (nearest {nearest}, cited {claimed_offset})"
        )
        return SpanCheck(True, nearest, ok, note)

    nq = _norm(q)
    nd = norm_doc if norm_doc is not None else _norm(doc_text)
    if nq and nq in nd:
        return SpanCheck(
            True,
            None,
            None,
            "normalized match (whitespace/case differs); offset unverifiable",
        )

    return SpanCheck(
        False,
        None,
        None,
        "quote not found in document — possible hallucinated citation",
    )


@dataclass(frozen=True)
class SpanRepair:
    """Result of snapping a cited quote to the document's own text.

    ``span`` — the repaired quote: the EXACT verbatim substring of the document when one was
    recovered, else the original quote unchanged. ``faithful`` — whether ``span`` is now a verbatim
    substring of the document (so the add-in can locate and redline it). ``method`` — how it
    matched: ``exact`` | ``normalized`` | ``fuzzy`` | ``empty`` | ``unfaithful``.
    """

    span: str
    faithful: bool
    method: str
    note: str


#: Conservative gates for the fuzzy fallback. A redline drives a tracked deletion, so snapping to
#: the WRONG clause is worse than leaving the finding advisory — only accept a single, near-verbatim
#: region (e.g. the model dropped/added a word). Below this, return unfaithful and let the UI show
#: the suggestion as manual guidance.
_FUZZY_MIN_LEN = 16
_FUZZY_MIN_RATIO = 0.90
_FUZZY_MIN_COVERAGE = 0.80


def repair_span(doc_text: str, quote: str, *, allow_fuzzy: bool = True) -> SpanRepair:
    """Snap a model-cited ``quote`` to the exact verbatim substring of ``doc_text`` it refers to.

    A model often quotes a clause that IS present but with cosmetic drift (curly vs straight
    quotes, NBSP, spacing, case) or a tiny paraphrase, which makes a naive substring check fail and
    a genuinely-present clause look hallucinated. This recovers the real document text so the Word
    add-in can locate and redline it. On no confident match the original quote is returned with
    ``faithful=False`` (advisory only — never a guess that could redline the wrong clause).
    """
    q = _strip_zw((quote or "").strip())
    if not q:
        return SpanRepair(quote or "", False, "empty", "empty quote")

    # 1. Already verbatim — nothing to repair.
    if q in doc_text:
        return SpanRepair(q, True, "exact", "exact match")

    # 2. Deterministic recovery: the quote matches under quote/dash/space/case folding, so pull the
    #    exact raw substring that the normalized match covers. High precision — never a guess.
    dn, dmap = _norm_with_map(doc_text)
    qn, _ = _norm_with_map(q)
    if qn:
        pos = dn.find(qn)
        if pos != -1:
            raw_start = dmap[pos]
            raw_end = dmap[pos + len(qn) - 1] + 1
            return SpanRepair(
                doc_text[raw_start:raw_end],
                True,
                "normalized",
                "recovered exact substring (quotes/spacing/case differed)",
            )

    # 3. Conservative fuzzy: a near-verbatim quote (e.g. one word off). Snap ONLY to a single,
    #    high-similarity region so we never redline the wrong clause; otherwise leave it advisory.
    if allow_fuzzy and len(qn) >= _FUZZY_MIN_LEN:
        sm = difflib.SequenceMatcher(None, dn, qn, autojunk=False)
        blocks = [b for b in sm.get_matching_blocks() if b.size > 0]
        if blocks:
            lo, hi = blocks[0].a, blocks[-1].a + blocks[-1].size
            cand = dn[lo:hi]
            ratio = difflib.SequenceMatcher(None, cand, qn, autojunk=False).ratio()
            coverage = sum(b.size for b in blocks) / len(qn)
            if (
                hi > lo
                and ratio >= _FUZZY_MIN_RATIO
                and coverage >= _FUZZY_MIN_COVERAGE
            ):
                raw_start = dmap[lo]
                raw_end = dmap[hi - 1] + 1
                return SpanRepair(
                    doc_text[raw_start:raw_end],
                    True,
                    "fuzzy",
                    f"approximate match (similarity {ratio:.2f})",
                )

    return SpanRepair(
        quote or "",
        False,
        "unfaithful",
        "quote not found in document — possible hallucinated citation",
    )
