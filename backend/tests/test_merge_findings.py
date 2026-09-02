"""Pure-logic tests for ``merge_findings`` and ``_dedupe_key`` in app/agents/reviewer.py.

``merge_findings`` dedupes ONE reviewer's raw findings by ``_dedupe_key`` (heading + normalized
span, falling back to title) — plan §4.2's merge step ("dedupe by (clause_heading,span)"). The
old two-list union (a per-clause pass merged with a whole-doc recall pass) no longer applies: the
per-clause pipeline was deleted and there is exactly one reviewer source per review, so this now
dedupes a single list, keeping the FIRST occurrence of each key.

Covered:
  * ``_dedupe_key`` normalization: whitespace collapse, lowercasing, span/title fallback, and the
    degenerate ``"|"`` key;
  * disjoint findings -> all kept, in order;
  * two findings sharing a key -> deduped to one, the first kept;
  * distinct issues on the SAME clause heading both survive (recall gain);
  * the degenerate ``"|"`` key is NEVER deduped (no heading + no span/title).
"""

from __future__ import annotations

from app.agents.reviewer import _dedupe_key, merge_findings

# --- _dedupe_key -------------------------------------------------------------


def test_key_collapses_whitespace_and_lowercases():
    f = {"clause_heading": "  Confidential\tInformation  ", "span": "Foo   Bar"}
    assert _dedupe_key(f) == "confidential information|foo bar"


def test_key_falls_back_to_title_when_no_span():
    f = {"clause_heading": "Term", "title": "Shortened Survival"}
    assert _dedupe_key(f) == "term|shortened survival"


def test_key_prefers_span_over_title():
    f = {"clause_heading": "Term", "span": "the span", "title": "the title"}
    assert _dedupe_key(f) == "term|the span"


def test_key_degenerate_when_empty():
    assert _dedupe_key({}) == "|"
    assert _dedupe_key({"clause_heading": "", "span": ""}) == "|"


# --- merge_findings: disjoint -------------------------------------------------


def test_disjoint_findings_all_kept_in_order():
    findings = [
        {"clause_heading": "A", "span": "alpha", "id": 1},
        {"clause_heading": "B", "span": "bravo", "id": 2},
    ]
    merged = merge_findings(findings)
    assert [f["id"] for f in merged] == [1, 2]


# --- merge_findings: dedup / preference ---------------------------------------


def test_same_key_deduped_to_first():
    findings = [
        {"clause_heading": "Term", "span": "five (5) years", "severity": "high"},
        {"clause_heading": "Term", "span": "five (5) years", "severity": "low"},
    ]
    merged = merge_findings(findings)
    assert len(merged) == 1
    assert merged[0]["severity"] == "high"  # the first occurrence wins


def test_overlap_matches_despite_whitespace_and_case():
    findings = [
        {"clause_heading": "Term", "span": "Five (5)  Years", "id": 1},
        {"clause_heading": " term ", "span": "five (5) years", "id": 2},
    ]
    merged = merge_findings(findings)
    assert len(merged) == 1
    assert merged[0]["id"] == 1


def test_distinct_issues_same_heading_both_survive():
    # Two DISTINCT spans on the same clause heading are NOT duplicates.
    findings = [
        {"clause_heading": "Term", "span": "survival period"},
        {"clause_heading": "Term", "span": "governing law"},
    ]
    merged = merge_findings(findings)
    assert len(merged) == 2
    spans = {f["span"] for f in merged}
    assert spans == {"survival period", "governing law"}


def test_degenerate_key_never_deduped():
    # Two findings with no heading and no span/title share the degenerate key "|" but must
    # BOTH survive — it carries no identity.
    findings = [{"id": 1}, {"id": 2}]
    merged = merge_findings(findings)
    assert len(merged) == 2
    assert [f["id"] for f in merged] == [1, 2]


def test_empty_input():
    assert merge_findings([]) == []
    findings = [{"clause_heading": "A", "span": "a"}]
    assert merge_findings(findings) == findings


def test_input_not_mutated():
    findings = [
        {"clause_heading": "A", "span": "a"},
        {"clause_heading": "A", "span": "a"},  # duplicate -> dropped
    ]
    original_len = len(findings)
    merge_findings(findings)
    # merge_findings builds a NEW list; the caller's list is untouched.
    assert len(findings) == original_len
