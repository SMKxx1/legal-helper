"""Deterministic recall backstop for the v4 eval scorer (M1).

The LLM grader (Claude Haiku in ``scripts/v4_eval.score``) occasionally mis-scores a
TRUE catch as a miss — semantic-match noise that makes a real recall regression and a
grader flake look identical, so recall deltas between engine configs can't be trusted.

This module adds a high-precision, DETERMINISTIC signal: an expected deviation is
"caught" when the review text literally shares distinctive anchors with the answer-key
deviation — a specific duration the counterparty edited (e.g. ``3 years``, ``30 days``)
or several rare content words lifted from the actual counterparty edit — and ALL of the
evidence must land within a single finding segment (clause locality), so one finding's
number or words cannot credit a different deviation. Used as an OR-backstop
(``caught = grader OR deterministic``) it is built to RESCUE true catches the grader missed
without inventing one, so measured recall moves toward truth. ``recall_det`` is reported
beside ``recall_llm`` so any divergence stays visible rather than silently trusted.

Precision is the design goal, not recall of the matcher itself: when the evidence is
weak it returns False and lets the LLM grader carry that deviation. Pure stdlib — no
API, no env, no provider imports — so it is unit-testable and free to run.
"""

from __future__ import annotations

import re
import unicodedata

#: English function words + NDA boilerplate that appears in nearly every finding and so
#: carries no discriminative signal. Kept broad on purpose: the distinctive-token overlap
#: must stay high-precision, so only genuinely rare terms should survive the filter.
_STOP: frozenset[str] = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "for",
        "with",
        "on",
        "at",
        "by",
        "from",
        "as",
        "is",
        "are",
        "be",
        "been",
        "being",
        "that",
        "this",
        "these",
        "those",
        "such",
        "any",
        "all",
        "not",
        "no",
        "nor",
        "but",
        "if",
        "then",
        "than",
        "so",
        "it",
        "its",
        "their",
        "they",
        "them",
        "which",
        "who",
        "whom",
        "whose",
        "will",
        "shall",
        "may",
        "must",
        "can",
        "could",
        "would",
        "should",
        "has",
        "have",
        "had",
        "was",
        "were",
        "into",
        "upon",
        "within",
        "without",
        "under",
        "over",
        "after",
        "before",
        "during",
        "per",
        "each",
        "other",
        "more",
        "most",
        "also",
        "only",
        "same",
        "both",
        "either",
        "neither",
        "what",
        "when",
        "where",
        "while",
        "because",
        "about",
        "against",
        "out",
        "off",
        "down",
        "up",
        "out",
        "via",
        "vs",
        "etc",
        "ie",
        "eg",
        "confidential",
        "confidentiality",
        "information",
        "party",
        "parties",
        "receiving",
        "disclosing",
        "recipient",
        "discloser",
        "agreement",
        "clause",
        "clauses",
        "section",
        "provision",
        "provisions",
        "term",
        "terms",
        "obligation",
        "obligations",
        "amperesand",
        "standard",
        "playbook",
        "template",
        "review",
        "finding",
        "findings",
        "severity",
        "deviation",
        "counterparty",
        "document",
        "documents",
        "right",
        "rights",
        "include",
        "includes",
        "including",
        "pursuant",
        "hereto",
        "herein",
        "hereof",
        "thereof",
        "therein",
        "whereof",
        "between",
        "relating",
        "related",
        "respect",
        "subject",
        "applicable",
        "required",
        "requirement",
        "requirements",
        "use",
        "used",
        "uses",
        "purpose",
        "purposes",
        "written",
        "writing",
        "date",
        "dates",
        "time",
        "make",
        "makes",
        "made",
        "shall_be",
    ]
)

_WS = re.compile(r"\s+")
_WORD = re.compile(r"[a-z][a-z0-9\-]{2,}")
#: A number in digit form (incl. a parenthetical "(30)" or a word-then-digit "three (3)")
#: immediately followed by a duration unit — the most discriminative anchor in an NDA edit.
_DUR = re.compile(
    r"\(?\b(\d{1,3}(?:,\d{3})+|\d{1,4})\)?[\s-]*(year|years|month|months|day|days|week|weeks)\b"
)
#: A duration's OWN component words (units + spelled-out numbers). Excluded from a duration finding's
#: corroboration so the borrowed (number, unit) can't self-satisfy the locality check — else an
#: unrelated clause that merely shares the same duration is wrongly credited (a det false positive).
_UNIT_WORDS = frozenset(
    {"year", "years", "month", "months", "day", "days", "week", "weeks"}
)
_NUMBER_WORDS = frozenset(
    {
        "zero",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
        "twenty",
        "thirty",
        "forty",
        "fifty",
        "sixty",
        "seventy",
        "eighty",
        "ninety",
        "hundred",
        "thousand",
    }
)

#: How many distinct content-word anchors must co-occur for a (non-numeric) catch. Three
#: genuinely rare shared legal terms is strong, length-independent evidence; two is enough
#: only when the clause-area keyword also matches (see ``det_caught``).
_STRONG_HITS = 3
_WEAK_HITS = 2


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    for a, b in (
        ("“", '"'),
        ("”", '"'),
        ("’", "'"),
        ("‘", "'"),
        ("–", "-"),
        ("—", "-"),
    ):
        s = s.replace(a, b)
    return _WS.sub(" ", s.lower()).strip()


def _unit(u: str) -> str:
    return u.rstrip("s")  # years -> year, days -> day


def _durations(text: str) -> set[tuple[str, str]]:
    """Distinctive (number, unit) duration anchors, e.g. {('3','year'), ('30','day')}."""
    return {(n.replace(",", ""), _unit(u)) for n, u in _DUR.findall(_norm(text))}


def _content_tokens(text: str) -> set[str]:
    """Rare, discriminative words from ``text`` (boilerplate/stopwords removed)."""
    return {w for w in _WORD.findall(_norm(text)) if w not in _STOP and not w.isdigit()}


def _clause_keywords(clause_type: str) -> set[str]:
    """Clause-area keywords from a snake_case clause_type, e.g.
    'term_of_confidentiality' -> {'term', 'confidentiality'}. Kept WITHOUT the boilerplate
    filter (the clause names ARE boilerplate words) but length-gated to stay meaningful."""
    return {
        w
        for w in re.split(r"[^a-z]+", (clause_type or "").lower())
        if len(w) >= 4 and w != "with"
    }


def _segments(review_text: str) -> list[str]:
    """The locality unit for matching. In the eval review_text every finding is its own line
    ("[SEV] title: rationale", "[MISSING] ...", "[CROSS] ..."), so a line is ONE issue.
    Anchors must co-occur within a single segment, which stops one finding's number or words
    from crediting a DIFFERENT deviation — the cross-deviation leak the matcher must avoid."""
    return [ln for ln in (review_text or "").splitlines() if ln.strip()]


def det_caught(deviation: dict, review_text: str) -> bool:
    """High-precision deterministic verdict: did ``review_text`` catch this deviation?

    The evidence must land within a SINGLE finding segment (clause locality): either the exact
    counterparty-edited (number, unit) duration appears as a real duration in that segment AND
    is corroborated by a distinctive edit word or the clause area, OR >=3 distinctive edit
    words co-occur in the segment (>=2 with a clause-area keyword). Otherwise False — the LLM
    grader carries it. Biased hard toward precision: a det false POSITIVE would inflate measured
    recall and mask a regression, so weak or cross-segment evidence never counts (a det false
    NEGATIVE is harmless — the grader still scores that deviation).

    Known limitation: within-segment matching is bag-of-words, so it is negation-insensitive
    (a flagged 'may NOT disclose...' shares words with a 'may disclose...' deviation). Rare and
    bounded by locality; recall_det is reported beside recall_llm so divergence stays visible.
    """
    excerpt = deviation.get("variant_excerpt") or ""
    dur_anchors = _durations(excerpt)
    tok_anchors = _content_tokens(excerpt)
    ckw = _clause_keywords(deviation.get("clause_type", ""))
    if not (dur_anchors or tok_anchors):
        return False

    for seg in _segments(review_text):
        seg_norm = _norm(seg)
        if not seg_norm:
            continue
        seg_tokens = set(_WORD.findall(seg_norm))
        hits = sum(1 for a in tok_anchors if a in seg_tokens)
        ckw_hit = bool(ckw & seg_tokens)
        # (1) Duration anchor: the SAME (number, unit) parsed as a REAL duration in THIS segment
        # (exact set match), corroborated by a distinctive edit word OUTSIDE the duration itself (its
        # own unit/number words would otherwise self-satisfy hits>=1) or the clause area — so a number
        # borrowed from another clause's finding can't credit this one.
        if dur_anchors & _durations(seg):
            corrob = sum(
                1
                for a in tok_anchors
                if a in seg_tokens and a not in _UNIT_WORDS and a not in _NUMBER_WORDS
            )
            # ckw must ALSO exclude the duration's own words — a clause_type like "ninety_day_notice"
            # would otherwise self-corroborate via 'ninety'/'day' present in the borrowed duration.
            ckw_corrob = bool((ckw - _UNIT_WORDS - _NUMBER_WORDS) & seg_tokens)
            if corrob >= 1 or ckw_corrob:
                return True
        # (2) Distinctive content-word overlap within this single finding segment.
        if hits >= _STRONG_HITS:
            return True
        if hits >= _WEAK_HITS and ckw_hit:
            return True
    return False


def det_recall(deviations: list[dict], review_text: str) -> dict:
    """Deterministic recall over the NON-probe expected deviations.

    Returns ``{caught_ids, recall, expected}``. ``caught_ids`` is meant to be UNIONed with
    the LLM grader's caught set; the combined recall is the trustworthy headline number.
    """
    non_probe = [d for d in deviations if not d.get("is_probe")]
    caught_ids = {d["id"] for d in non_probe if det_caught(d, review_text)}
    return {
        "caught_ids": caught_ids,
        "recall": round(len(caught_ids) / len(non_probe), 3) if non_probe else 0.0,
        "expected": len(non_probe),
    }
