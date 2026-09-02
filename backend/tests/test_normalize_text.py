"""Pure-logic tests for the Tier-1 cache-normalization helpers (engine.simcache).

Covers ``normalize_text`` (NFKC + lowercase + formatting-punctuation strip with the
meaning-bearing ``_KEEP`` symbols preserved + whitespace collapse) and ``norm_sha256``
(sha256 of the normalized text, empty string for content-free docs). These are the live
helpers backing the document-reuse cache key in create_review; they need no DB/provider.

Key invariants exercised:
- texts differing ONLY by whitespace/case/unicode-form normalize EQUAL,
- materially different texts (incl. a one-symbol legal-meaning change) do NOT,
- norm_sha256 is stable, equal for normalize-equal inputs, differs otherwise, and is
  "" for symbol/whitespace-only (content-free) input.
"""

from __future__ import annotations

import hashlib

from app.engine.simcache import norm_sha256, normalize_text

# --- normalize_text: whitespace + case collapse -----------------------------------


def test_whitespace_only_difference_normalizes_equal():
    a = "The quick brown fox"
    b = "  The\tquick\n\nbrown   fox  "
    assert normalize_text(a) == normalize_text(b)
    assert normalize_text(a) == "the quick brown fox"


def test_case_only_difference_normalizes_equal():
    assert normalize_text("CONFIDENTIAL Information") == normalize_text(
        "confidential information"
    )


def test_combined_whitespace_and_case_normalize_equal():
    pdf_like = "DISCLOSING   Party\n\tshall NOT disclose"
    docx_like = "disclosing party shall not disclose"
    assert normalize_text(pdf_like) == normalize_text(docx_like)


def test_leading_trailing_whitespace_stripped():
    assert normalize_text("   hello world   ") == "hello world"


# --- normalize_text: punctuation stripping ----------------------------------------


def test_formatting_punctuation_stripped_to_space():
    # Commas/periods/parens/quotes are extraction artifacts -> removed (become spaces),
    # leaving only the words, whitespace-collapsed.
    assert normalize_text('Hello, "world" (test).') == "hello world test"


def test_punctuation_difference_only_normalizes_equal():
    a = "term: two years, ending."
    b = "term two years ending"
    assert normalize_text(a) == normalize_text(b)


# --- normalize_text: meaning-bearing symbols preserved (_KEEP) ---------------------


def test_percent_symbol_preserved():
    assert normalize_text("cap of 5%") == "cap of 5%"
    # "5%" and bare "5" must NOT collapse to the same key.
    assert normalize_text("cap of 5%") != normalize_text("cap of 5")


def test_currency_and_comparison_symbols_preserved():
    assert normalize_text("$5m") == "$5m"
    assert normalize_text("<= 30 days") == "<= 30 days"
    # comparison operator carries meaning -> must differ from the bare phrase
    assert normalize_text("<= 30 days") != normalize_text("30 days")


def test_each_keep_symbol_survives_normalization():
    for sym in "%$€£¥<>=±≤≥≠−×÷‰":
        out = normalize_text(f"x {sym} y")
        assert sym in out, f"meaning-bearing symbol {sym!r} was stripped"


# --- normalize_text: NFKC unicode canonicalization --------------------------------


def test_nfc_vs_nfd_normalize_equal():
    # "café" composed (NFC) vs decomposed (NFD: e + combining acute) -> same key.
    nfc = "café"  # é as one code point
    nfd = "café"  # e + U+0301 combining acute
    assert nfc != nfd  # genuinely different byte sequences
    assert normalize_text(nfc) == normalize_text(nfd)


def test_nfkc_folds_compatibility_forms():
    # Fullwidth "ＡＢ" folds to ascii "ab" under NFKC + lowercase.
    assert normalize_text("ＡＢ") == "ab"


# --- normalize_text: material differences stay distinct ---------------------------


def test_one_word_legal_change_stays_distinct():
    assert normalize_text("the receiving party shall not disclose") != normalize_text(
        "the receiving party shall disclose"
    )


def test_different_content_normalizes_distinct():
    assert normalize_text("two years") != normalize_text("twenty years")


# --- normalize_text: empty / content-free inputs ----------------------------------


def test_empty_and_none_normalize_to_empty():
    assert normalize_text("") == ""
    assert normalize_text(None) == ""  # type: ignore[arg-type]


def test_whitespace_only_normalizes_to_empty():
    assert normalize_text("   \t\n  ") == ""


def test_punctuation_only_normalizes_to_empty():
    # No word chars, no _KEEP symbols -> content-free.
    assert normalize_text(".,;:!?()-[]") == ""


# --- norm_sha256 ------------------------------------------------------------------


def test_norm_sha256_matches_manual_hash():
    text = "Confidential, Information."
    expected = hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()
    assert norm_sha256(text) == expected


def test_norm_sha256_is_hex_digest_of_expected_length():
    digest = norm_sha256("some real content here")
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_norm_sha256_stable_across_calls():
    text = "Stable input text"
    assert norm_sha256(text) == norm_sha256(text)


def test_norm_sha256_equal_for_normalize_equal_inputs():
    # Differ only by whitespace/case/punctuation -> same cache key.
    assert norm_sha256("  THE  Quick, Brown.  Fox ") == norm_sha256(
        "the quick brown fox"
    )


def test_norm_sha256_differs_for_material_change():
    assert norm_sha256("shall not disclose") != norm_sha256("shall disclose")


def test_norm_sha256_differs_for_kept_symbol():
    assert norm_sha256("cap of 5%") != norm_sha256("cap of 5")


def test_norm_sha256_empty_for_content_free_doc():
    # Content-free docs return "" so they never key into the cache.
    assert norm_sha256("") == ""
    assert norm_sha256("   \n\t ") == ""
    assert norm_sha256(".,;:!?") == ""
