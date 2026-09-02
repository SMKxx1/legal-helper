"""Pure-logic unit tests for span faithfulness + repair (``app.engine.spans``).

``check_span`` answers "is this cited quote present in the document?" (now tolerant of curly vs
straight quotes and dashes as well as whitespace/case/zero-width). ``repair_span`` goes further: it
snaps the quote to the EXACT verbatim substring of the document so the Word add-in can locate and
redline it — recovering from cosmetic drift and a conservative one-word slip, while refusing to
guess on a genuine paraphrase (which would risk redlining the wrong clause).

Imports the module directly; no app/DB/provider fixtures needed.
"""

from __future__ import annotations

from app.engine.spans import check_span, repair_span

# Note the curly quotes around "Disclosing Party" and the em dash — drift the model often flattens.
DOC = (
    "The Receiving Party shall hold in strict confidence and not disclose to any third party any "
    "Confidential Information of the “Disclosing Party” — except as approved in "
    "writing by the Disclosing Party."
)


def _verbatim(span: str) -> bool:
    """The whole point of a repair: the result is a real substring the add-in can find."""
    return bool(span) and span in DOC


# --------------------------------------------------------------------------- #
# check_span — existence
# --------------------------------------------------------------------------- #
def test_check_span_exact():
    assert check_span(DOC, "hold in strict confidence").faithful


def test_check_span_folds_smart_quotes():
    # straight quotes in the cited span vs curly quotes in the document — now judged faithful
    assert check_span(DOC, 'Information of the "Disclosing Party"').faithful


def test_check_span_folds_dash():
    # the document uses an em dash; the cited span uses a plain hyphen
    assert check_span(DOC, "- except as approved in writing").faithful


def test_check_span_folds_whitespace_and_case():
    assert check_span(DOC, "HOLD   in  Strict   Confidence").faithful


def test_check_span_rejects_absent():
    chk = check_span(DOC, "indemnify and hold harmless from all third-party claims")
    assert not chk.faithful
    assert "not found" in chk.note


# --------------------------------------------------------------------------- #
# repair_span — snap to verbatim
# --------------------------------------------------------------------------- #
def test_repair_exact_unchanged():
    r = repair_span(DOC, "hold in strict confidence")
    assert r.faithful and r.method == "exact"
    assert r.span == "hold in strict confidence"


def test_repair_recovers_smart_quote_verbatim():
    r = repair_span(DOC, 'Information of the "Disclosing Party"')  # straight quotes
    assert r.faithful and r.method == "normalized"
    assert _verbatim(r.span)  # recovered the document's actual text…
    assert (
        '"' not in r.span
    )  # …with the doc's curly quotes, not the model's straight ones
    assert "Disclosing Party" in r.span


def test_repair_recovers_whitespace_and_case_verbatim():
    r = repair_span(DOC, "HOLD IN   STRICT confidence")
    assert r.faithful and r.method == "normalized"
    assert r.span == "hold in strict confidence"  # the document's own text/case
    assert _verbatim(r.span)


def test_repair_strips_zero_width():
    r = repair_span(DOC, "hold in​ strict confidence")  # ZWSP mid-quote
    assert r.faithful
    assert _verbatim(r.span)


def test_repair_fuzzy_snaps_one_word_slip():
    # the model wrote "absolute" where the document says "strict"
    r = repair_span(
        DOC, "shall hold in absolute confidence and not disclose to any third party"
    )
    assert r.faithful and r.method == "fuzzy"
    assert _verbatim(r.span)
    assert (
        "strict" in r.span and "absolute" not in r.span
    )  # snapped to the document's word


def test_repair_refuses_paraphrase():
    r = repair_span(
        DOC, "the receiver must keep all materials secret from everyone forever"
    )
    assert not r.faithful and r.method == "unfaithful"
    # original quote returned unchanged so the UI can still show it as advisory
    assert r.span == "the receiver must keep all materials secret from everyone forever"


def test_repair_no_fuzzy_flag_leaves_slip_unfaithful():
    # whole-doc passes allow_fuzzy=False: a one-word slip must NOT be silently snapped
    r = repair_span(
        DOC,
        "shall hold in absolute confidence and not disclose to any third party",
        allow_fuzzy=False,
    )
    assert not r.faithful


def test_repair_empty():
    r = repair_span(DOC, "   ")
    assert not r.faithful and r.method == "empty"
    assert r.span == "   "  # original returned (caller decides what to do)


def test_repair_index_map_survives_lower_expanding_char():
    # str.lower() maps İ (U+0130) to TWO chars ("i" + combining dot); the index map must stay in
    # sync so a match AFTER it recovers the correct raw slice. Regression: a desynced map produced
    # a slice shifted by one char (silently faithful) or an IndexError near the document end.
    doc = "İstanbul Co. shall hold in strict confidence all Confidential Information hereunder."
    r = repair_span(
        doc, "HOLD IN STRICT confidence all confidential information"
    )  # case drift
    assert r.faithful
    assert (
        r.span == "hold in strict confidence all Confidential Information"
    )  # exact, not shifted
    # a match running to the very end must not IndexError
    r2 = repair_span(doc, "Confidential Information hereunder")
    assert r2.faithful and r2.span == "Confidential Information hereunder"
