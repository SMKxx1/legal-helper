"""Pure-logic unit tests for clause alignment (``app.review.alignment.align_clauses``).

``align_clauses`` pairs a company-template clause list against an incoming NDA's clauses and
returns ONLY *material* pairs, each a :class:`ClausePair` carrying a ``change_type`` of
``"modification"`` | ``"addition"`` | ``"deletion"`` plus a difflib ``similarity``. Matched pairs
whose wording is effectively identical (sim >= ``unchanged_threshold``) are dropped, so two identical
lists yield an EMPTY result. Covered here: identical lists (all unchanged -> dropped), a heading-
matched modification, an incoming-only addition, a template-only deletion, and the
``unchanged_threshold=1.0`` knob that promotes a near-but-not-exact pair from "unchanged" to a
returned modification.

Imports the module directly; no app/DB/provider fixtures needed.
"""

from __future__ import annotations

from app.ingestion.segmenter import Clause
from app.redline.differ import similarity
from app.review.alignment import ClausePair, align_clauses


def _clause(index: int, heading: str, text: str, number: str = "") -> Clause:
    """Build a Clause with offsets that don't influence alignment logic."""
    return Clause(index=index, number=number or str(index), heading=heading, text=text)


# A reasonably long body so a one-token edit stays above the 0.985 "unchanged" floor.
_LONG_BODY = (
    "The Receiving Party shall hold all Confidential Information in strict confidence and shall "
    "not disclose any such information to any third party without the prior written consent of "
    "the Disclosing Party, and shall use the Confidential Information solely for the Purpose."
)


def test_identical_lists_are_all_unchanged_and_dropped() -> None:
    """Identical template/incoming -> every pair is effectively identical, so NONE are material.

    Characterization: unchanged pairs are NOT returned, so identical lists give an empty list
    (the function never emits an "unchanged" ClausePair).
    """
    clauses = [
        _clause(
            1,
            "Definitions",
            "Confidential Information means all non-public information.",
        ),
        _clause(2, "Term", _LONG_BODY),
    ]
    # Independent copies so identity (not object reuse) drives the match.
    template = [_clause(c.index, c.heading, c.text) for c in clauses]
    incoming = [_clause(c.index, c.heading, c.text) for c in clauses]

    result = align_clauses(template, incoming)

    assert result == []


def test_modified_clause_is_a_modification() -> None:
    """Same heading, materially different body -> one ClausePair with change_type 'modification'."""
    template = [
        _clause(
            1,
            "Term",
            "This Agreement remains in effect for a period of one (1) year from the Effective Date.",
        )
    ]
    incoming = [
        _clause(
            1,
            "Term",
            "This Agreement remains in effect for a period of ten (10) years from the Effective Date.",
        )
    ]

    result = align_clauses(template, incoming)

    assert len(result) == 1
    pair = result[0]
    assert pair.change_type == "modification"
    assert pair.template is template[0]
    assert pair.incoming is incoming[0]
    # The reported similarity is the actual difflib ratio of the two bodies: matched but not
    # identical, hence below the default unchanged threshold and above the match floor.
    expected_sim = similarity(template[0].text, incoming[0].text)
    assert pair.similarity == expected_sim
    assert 0.45 <= pair.similarity < 0.985


def test_added_incoming_clause_is_an_addition() -> None:
    """A clause present only in the incoming document -> change_type 'addition', no template side."""
    incoming = [
        _clause(
            1,
            "Indemnification",
            "The Receiving Party shall indemnify the Disclosing Party.",
        )
    ]

    result = align_clauses([], incoming)

    assert len(result) == 1
    pair = result[0]
    assert pair.change_type == "addition"
    assert pair.template is None
    assert pair.incoming is incoming[0]
    assert pair.similarity == 0.0


def test_removed_template_clause_is_a_deletion() -> None:
    """A clause present only in the template -> change_type 'deletion', no incoming side."""
    template = [
        _clause(
            1,
            "Non-Solicitation",
            "Neither party shall solicit the employees of the other.",
        )
    ]

    result = align_clauses(template, [])

    assert len(result) == 1
    pair = result[0]
    assert pair.change_type == "deletion"
    assert pair.template is template[0]
    assert pair.incoming is None
    assert pair.similarity == 0.0


def test_addition_and_deletion_together_preserve_order() -> None:
    """One matched-unchanged clause is dropped while the added/removed structural pairs surface.

    Incoming-only clauses are emitted in incoming order; template-only deletions are appended after.
    """
    shared = _clause(
        1, "Definitions", "Confidential Information means all non-public information."
    )
    template = [
        _clause(shared.index, shared.heading, shared.text),
        _clause(
            2, "Governing Law", "This Agreement is governed by the laws of Delaware."
        ),
    ]
    incoming = [
        _clause(shared.index, shared.heading, shared.text),
        _clause(
            2,
            "Force Majeure",
            "Neither party is liable for delays caused by events of war.",
        ),
    ]

    result = align_clauses(template, incoming)

    # The identical "Definitions" pair is unchanged -> dropped. We keep one addition + one deletion.
    change_types = [p.change_type for p in result]
    assert change_types == ["addition", "deletion"]
    addition, deletion = result
    assert addition.incoming is incoming[1]
    assert addition.template is None
    assert deletion.template is template[1]
    assert deletion.incoming is None


def test_unchanged_threshold_1_0_promotes_near_identical_to_modification() -> None:
    """With threshold 1.0 only byte-identical pairs are dropped; a one-char edit becomes material.

    Mirrors the redlines-only scope where the two sides are the same document (changes accepted vs
    rejected): an untouched clause is exactly identical, but a minor term edit must still be
    reviewed even though its similarity is very close to 1.0.
    """
    template = [_clause(1, "Term", _LONG_BODY)]
    # Append a single character so the bodies differ minimally (ratio just under 1.0).
    incoming = [_clause(1, "Term", _LONG_BODY + ".")]

    near_sim = similarity(template[0].text, incoming[0].text)
    # Sanity: the edit is near-identical (above the default 0.985 floor) yet not exact.
    assert 0.985 < near_sim < 1.0

    # Default threshold (0.985) treats this as unchanged and drops it.
    assert align_clauses(template, incoming) == []

    # threshold=1.0 requires byte-identity to be "unchanged"; this near-miss is a modification.
    promoted = align_clauses(template, incoming, unchanged_threshold=1.0)
    assert len(promoted) == 1
    assert promoted[0].change_type == "modification"
    assert promoted[0].similarity == near_sim


def test_high_cosine_but_low_difflib_pair_is_not_dropped_as_unchanged() -> None:
    """Escalate-only invariant: an injected ``text_sim_fn`` (an embedding cosine) is used for
    PAIRING only. The unchanged-DROP decision must always use the difflib ratio, so a
    reworded-but-semantically-close pair whose cosine is ~1.0 (well above 0.985) but whose difflib
    ratio is below the threshold must still surface as a modification — never silently dropped.
    An embedding may only ADD review work, never remove it.
    """
    template = [
        _clause(
            1,
            "Confidentiality",
            "The Receiving Party shall keep all Confidential Information strictly secret.",
        )
    ]
    # Same clause, heavily reworded: a human/embedding reads it as near-identical, but difflib's
    # char-ratio is far below the 0.985 unchanged floor.
    incoming = [
        _clause(
            1,
            "Confidentiality",
            "Recipient must hold every item of proprietary data in complete confidence.",
        )
    ]

    difflib_sim = similarity(template[0].text, incoming[0].text)
    assert difflib_sim < 0.985  # difflib alone would NOT call this unchanged

    # A fake cosine that reports near-identity for everything (like an embedding on a reword).
    def fake_cosine(_a: str, _b: str) -> float:
        return 0.999

    result = align_clauses(template, incoming, text_sim_fn=fake_cosine)

    # The high cosine must not let the pair clear the unchanged bar: it stays a reviewed modification.
    assert len(result) == 1
    assert result[0].change_type == "modification"
    assert result[0].template is template[0]
    assert result[0].incoming is incoming[0]


def test_clausepair_default_similarity_is_zero() -> None:
    """Sanity-check the ClausePair shape used throughout: similarity defaults to 0.0."""
    pair = ClausePair(change_type="addition", template=None, incoming=None)
    assert pair.similarity == 0.0
