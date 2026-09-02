"""Pure-logic tests for ``merge_findings`` and ``_key`` in app/engine/wholedoc.py.

``merge_findings`` unions per-clause findings with whole-doc findings, deduping
overlaps by ``_key`` (heading + normalized span/title prefix) and preferring the
per-clause finding (first arg) on overlap. ``_key`` builds the dedup identity:
it whitespace-collapses + lowercases ``clause_heading`` and ``span`` (falling back
to ``title``), truncates the span part to 40 chars, and joins as ``"head|span"``.

Covered:
  * ``_key`` normalization: whitespace collapse, lowercasing, span/title fallback,
    40-char span truncation, and the degenerate ``"|"`` key.
  * disjoint findings -> all kept (order: clause findings first, then whole-doc);
  * two findings sharing a ``_key`` -> deduped to one, per-clause (first-arg) kept;
  * overlap prefers the per-clause finding's payload over the whole-doc one;
  * distinct issues on the SAME clause heading both survive (recall gain);
  * the degenerate ``"|"`` key is NEVER deduped (no heading + no span/title).
"""

from __future__ import annotations

from app.engine.wholedoc import _key, merge_findings

# --- _key ------------------------------------------------------------------


def test_key_collapses_whitespace_and_lowercases():
    f = {"clause_heading": "  Confidential\tInformation  ", "span": "Foo   Bar"}
    assert _key(f) == "confidential information|foo bar"


def test_key_falls_back_to_title_when_no_span():
    f = {"clause_heading": "Term", "title": "Shortened Survival"}
    assert _key(f) == "term|shortened survival"


def test_key_prefers_span_over_title():
    f = {"clause_heading": "Term", "span": "the span", "title": "the title"}
    assert _key(f) == "term|the span"


def test_key_truncates_span_to_40_chars():
    span = "x" * 100
    f = {"clause_heading": "h", "span": span}
    head, _, span_part = _key(f).partition("|")
    assert head == "h"
    assert span_part == "x" * 40


def test_key_degenerate_when_empty():
    assert _key({}) == "|"
    assert _key({"clause_heading": "", "span": ""}) == "|"


# --- merge_findings: disjoint ----------------------------------------------


def test_disjoint_findings_all_kept_in_order():
    clause = [{"clause_heading": "A", "span": "alpha", "source": "clause"}]
    whole = [{"clause_heading": "B", "span": "bravo", "source": "wholedoc"}]
    merged = merge_findings(clause, whole)
    assert len(merged) == 2
    # Clause findings come first, then the appended whole-doc finding.
    assert merged[0]["source"] == "clause"
    assert merged[1]["source"] == "wholedoc"


# --- merge_findings: dedup / preference -------------------------------------


def test_same_key_deduped_to_one():
    clause = [{"clause_heading": "Term", "span": "five (5) years", "source": "clause"}]
    whole = [{"clause_heading": "Term", "span": "five (5) years", "source": "wholedoc"}]
    merged = merge_findings(clause, whole)
    assert len(merged) == 1
    assert merged[0]["source"] == "clause"


def test_overlap_prefers_per_clause_payload():
    # Same _key, but different payloads — the per-clause finding must win.
    clause = [
        {
            "clause_heading": "Confidential Information",
            "span": "shall mean",
            "severity": "high",
            "source": "clause",
        }
    ]
    whole = [
        {
            "clause_heading": "Confidential Information",
            "span": "shall mean",
            "severity": "low",
            "source": "wholedoc",
        }
    ]
    merged = merge_findings(clause, whole)
    assert len(merged) == 1
    assert merged[0]["severity"] == "high"
    assert merged[0]["source"] == "clause"


def test_overlap_matches_despite_whitespace_and_case():
    # _key normalization means these collide even though raw text differs.
    clause = [{"clause_heading": "Term", "span": "Five (5)  Years", "source": "clause"}]
    whole = [
        {"clause_heading": " term ", "span": "five (5) years", "source": "wholedoc"}
    ]
    merged = merge_findings(clause, whole)
    assert len(merged) == 1
    assert merged[0]["source"] == "clause"


def test_distinct_issues_same_heading_both_survive():
    # Two DISTINCT spans on the same clause heading are NOT duplicates.
    clause = [{"clause_heading": "Term", "span": "survival period", "source": "clause"}]
    whole = [{"clause_heading": "Term", "span": "governing law", "source": "wholedoc"}]
    merged = merge_findings(clause, whole)
    assert len(merged) == 2
    spans = {f["span"] for f in merged}
    assert spans == {"survival period", "governing law"}


def test_degenerate_key_never_deduped():
    # Two whole-doc findings with no heading and no span/title share _key "|" but
    # must BOTH survive — the degenerate key carries no identity.
    clause: list[dict] = []
    whole = [
        {"clause_heading": "", "title": "first issue without span", "id": 1},
        {"clause_heading": "", "title": "second issue without span", "id": 2},
    ]
    # title fallback gives them DISTINCT keys, so use truly empty findings to force "|".
    whole_empty = [{"id": 1}, {"id": 2}]
    merged = merge_findings(clause, whole_empty)
    assert len(merged) == 2
    assert [f["id"] for f in merged] == [1, 2]
    # And the title-bearing ones are distinct keys anyway -> both kept.
    assert len(merge_findings([], whole)) == 2


def test_degenerate_clause_does_not_swallow_wholedoc():
    # A degenerate "|" already in the clause set must not block a degenerate whole-doc.
    clause = [{"source": "clause"}]
    whole = [{"source": "wholedoc"}]
    merged = merge_findings(clause, whole)
    assert len(merged) == 2


def test_empty_inputs():
    assert merge_findings([], []) == []
    clause = [{"clause_heading": "A", "span": "a"}]
    assert merge_findings(clause, []) == clause
    whole = [{"clause_heading": "B", "span": "b"}]
    assert merge_findings([], whole) == whole


def test_inputs_not_mutated_aside_from_append_semantics():
    clause = [{"clause_heading": "A", "span": "a"}]
    whole = [{"clause_heading": "A", "span": "a"}]  # duplicate -> dropped
    original_clause_len = len(clause)
    merge_findings(clause, whole)
    # merge_findings builds a NEW list; the caller's clause list is untouched.
    assert len(clause) == original_clause_len
