"""Pure-logic unit tests for the deterministic recall backstop (``app.eval_scoring.det_caught``).

``det_caught`` is a HIGH-PRECISION deterministic verdict: did ``review_text`` literally catch a
deviation? Evidence (an exact (number, unit) duration anchor and/or distinctive content words from
the deviation's ``variant_excerpt``) must co-occur within a SINGLE finding segment (one non-blank
line). Coverage here:

- a review line that echoes the deviation's ``variant_excerpt`` -> True, via both the duration path
  (matching duration + a corroborating distinctive word/clause keyword) and the content-word path
  (>=3 distinctive overlaps, or >=2 with a clause-area keyword);
- empty / whitespace-only / unrelated review text -> False;
- a deviation with no usable anchors (empty excerpt) -> False;
- the LOCALITY rule: anchors spread across DIFFERENT lines never count, and a segment that shares
  ONLY the borrowed duration (number+unit) without a distinctive corroborator never counts.

Pure stdlib module — no app/DB/provider fixtures needed.
"""

from __future__ import annotations

from app.eval_scoring import det_caught

# --- duration-anchor path: review line echoes the variant_excerpt's duration + a distinct word ---


def test_duration_anchor_with_corroborating_word_is_caught():
    dev = {
        "variant_excerpt": "Confidentiality obligations survive for 3 years after termination.",
        "clause_type": "term_of_confidentiality",
    }
    review = (
        "[HIGH] Survival period: counterparty extended survival to 3 years; "
        "obligations survive after termination."
    )
    # ('3','year') duration matches in the segment AND 'survive'/'termination' corroborate.
    assert det_caught(dev, review) is True


def test_duration_anchor_corroborated_by_clause_area_only():
    # No distinctive edit word overlaps, but the clause-area keyword ('termination') lands in the
    # same segment as the matching duration -> ckw_corrob path returns True.
    dev = {
        "variant_excerpt": "shall remain in effect for 5 years",
        "clause_type": "termination_notice",
    }
    review = "[MED] Term bumped to 5 years on termination of the deal."
    assert det_caught(dev, review) is True


def test_duration_present_but_only_unit_word_shared_is_not_caught():
    # The segment borrows the SAME (3, year) duration but shares only the unit word 'years' — which is
    # excluded from corroboration — and no clause keyword. Precision guard: must NOT be credited.
    dev = {
        "variant_excerpt": "survives for 3 years",
        "clause_type": "",
    }
    review = "[LOW] Liability cap is set at 3 years of fees."
    assert det_caught(dev, review) is False


# --- content-word path: >=3 distinctive overlaps in one segment ---


def test_three_distinctive_words_in_one_segment_is_caught():
    dev = {
        "variant_excerpt": "Recipient may disclose to its affiliates, advisors, and financing sources.",
        "clause_type": "permitted_disclosures",
    }
    review = "[MEDIUM] Permitted disclosures: counterparty added affiliates, advisors and financing sources."
    assert det_caught(dev, review) is True


# --- content-word path: weak (2) overlaps rescued only by a clause-area keyword ---


def test_two_words_plus_clause_keyword_is_caught():
    dev = {
        "variant_excerpt": "may share with affiliates and advisors",
        "clause_type": "permitted_recipients",
    }
    # 'share' + 'affiliates' = 2 distinctive hits; 'permitted' is the clause-area keyword.
    review = "[LOW] Counterparty is now permitted to share with affiliates."
    assert det_caught(dev, review) is True


def test_two_words_without_clause_keyword_is_not_caught():
    # Same two overlaps, but clause_type gives no keyword -> weak evidence stays uncredited.
    dev = {
        "variant_excerpt": "may share with affiliates and advisors",
        "clause_type": "",
    }
    review = "[LOW] Counterparty is now allowed to share with affiliates."
    assert det_caught(dev, review) is False


# --- empty / unrelated / no-anchor cases ---


def test_empty_review_text_is_not_caught():
    dev = {
        "variant_excerpt": "survive for 3 years after termination",
        "clause_type": "term_of_confidentiality",
    }
    assert det_caught(dev, "") is False


def test_whitespace_only_review_text_is_not_caught():
    dev = {
        "variant_excerpt": "survive for 3 years after termination",
        "clause_type": "term_of_confidentiality",
    }
    assert det_caught(dev, "   \n\t\n   ") is False


def test_unrelated_review_text_is_not_caught():
    dev = {
        "variant_excerpt": "Recipient may disclose to its affiliates, advisors, and financing sources.",
        "clause_type": "permitted_disclosures",
    }
    review = "[HIGH] Governing law: counterparty switched the forum to Delaware courts."
    assert det_caught(dev, review) is False


def test_no_usable_anchors_is_not_caught():
    # Empty excerpt -> neither duration nor content anchors -> early False even if the review is rich.
    dev = {"variant_excerpt": "", "clause_type": "permitted_disclosures"}
    review = "[MED] affiliates advisors financing sources disclose 3 years termination"
    assert det_caught(dev, review) is False


def test_missing_variant_excerpt_key_is_not_caught():
    # deviation.get('variant_excerpt') -> None -> '' ; no anchors -> False, no KeyError.
    dev = {"clause_type": "permitted_disclosures"}
    review = "[MED] affiliates advisors financing"
    assert det_caught(dev, review) is False


# --- LOCALITY: matching evidence split across DIFFERENT segments must NOT count ---


def test_content_words_split_across_lines_is_not_caught():
    dev = {
        "variant_excerpt": "disclose to affiliates advisors financing sources",
        "clause_type": "",
    }
    # Five distinctive words, but no single line holds >=3 (and no clause keyword for the weak path).
    review = "\n".join(
        [
            "[A] counterparty may disclose to affiliates",
            "[B] also advisors and financing",
            "[C] external sources noted",
        ]
    )
    assert det_caught(dev, review) is False


def test_duration_and_corroborator_on_different_lines_is_not_caught():
    dev = {
        "variant_excerpt": "obligations survive for 3 years after closing",
        "clause_type": "",
    }
    # Duration on line A, the corroborating words on line B — locality forbids crediting across lines.
    review = "\n".join(
        [
            "[A] the headline term is 3 years",
            "[B] obligations survive past closing of the transaction",
        ]
    )
    assert det_caught(dev, review) is False


def test_same_words_collapsed_into_one_line_flips_to_caught():
    # Sanity contrast for the locality tests above: identical words, but now within ONE segment.
    dev = {
        "variant_excerpt": "disclose to affiliates advisors financing sources",
        "clause_type": "",
    }
    review = (
        "[A] counterparty may disclose to affiliates, advisors, financing and sources"
    )
    assert det_caught(dev, review) is True
