"""Deterministic keyword router matrix (reference §3.1/§6) — the ported ``Deterministic Route`` node.

Pure logic, zero I/O. Every routing behavior the ground-truth reference documents is pinned here:
bare help/review/archive/generate fire; greetings short-circuit to help; template keywords, envelope
keywords, and ≥2-deliverable messages DELIBERATELY defer to the LLM classifier (``None``).
"""

from __future__ import annotations

import pytest

from app.bot.router import Classification, deterministic_route, normalize

# --------------------------------------------------------------------------- #
# Bare commands fire their intent (reference §3.1 bareReview/bareArchive/bareGenerate + help)
# --------------------------------------------------------------------------- #
_FIRES = [
    # help + greetings (greetings short-circuit to help — reference §6)
    ("help", "help"),
    ("help me", "help"),
    ("what can you do", "help"),
    ("what can you do?", "help"),
    ("commands", "help"),
    ("how do you work", "help"),
    ("hi", "help"),
    ("hello", "help"),
    ("hey", "help"),
    ("hey there", "help"),
    ("hello team", "help"),
    ("good morning", "help"),
    ("", "help"),  # empty message → friendly help default
    ("   ", "help"),
    ("hi there, can you help?", "help"),
    # review (bare + natural single-deliverable phrasings)
    ("review", "review"),
    ("review this", "review"),
    ("review this nda", "review"),
    ("Review this NDA.", "review"),
    ("REVIEW!!!", "review"),
    ("please review the attached document", "review"),
    ("can you review this for me", "review"),
    ("review the agreement", "review"),
    ("please review this, thanks", "review"),
    ("give me feedback on this nda", "review"),
    # archive
    ("archive", "archive"),
    ("archive this", "archive"),
    ("archive this nda", "archive"),
    ("please archive the signed nda", "archive"),
    # generate
    ("generate", "generate"),
    ("generate an nda", "generate"),
    ("create an nda", "generate"),
    ("draft a new nda", "generate"),
    ("make an nda", "generate"),
    ("prepare an nda for acme", "generate"),
]

# --------------------------------------------------------------------------- #
# Deliberate defers to the classifier (reference §3.1/§6): template kw, envelope kw, ≥2 deliverables,
# and any message with no recognized deliverable keyword.
# --------------------------------------------------------------------------- #
_DEFERS = [
    # template keywords always defer (picker selectors are LLM-extracted)
    "template",
    "send me a template",
    "i need a blank nda",
    "can i get the us company template",
    "send me a copy of the nda",
    "sample nda please",
    "empty nda",
    # envelope keywords always defer (signer emails / order / cc timing are LLM-extracted)
    "send this for signature",
    "create a docusign envelope",
    "get these signed",
    "send to jane@x.com and bob@y.com for signature",
    "execute this nda",
    "i need signatures on this",
    # ≥2 deliverable keywords → ambiguous → defer
    "review and archive this",
    "generate and review",
    "review then archive",
    "help me generate an nda",
    "create and review the nda",
    # template + generate collision → template present → defer
    "make me a copy",
    "generate a template",
    # no recognized deliverable keyword → defer
    "what is the weather",
    "is our nda still valid",
    "random gibberish xyz",
    "mutual",  # a mutuality word alone is not a deliverable
    "us",  # a jurisdiction alone is not a deliverable
]


@pytest.mark.parametrize("text,intent", _FIRES)
def test_deterministic_fires_bare_command(text: str, intent: str) -> None:
    d = deterministic_route(text)
    assert d is not None, (
        f"{text!r} should fire {intent!r} deterministically, not defer"
    )
    assert d.intent == intent


@pytest.mark.parametrize("text", _DEFERS)
def test_deterministic_defers_to_classifier(text: str) -> None:
    assert deterministic_route(text) is None, f"{text!r} should defer to the classifier"


# --------------------------------------------------------------------------- #
# Output parity: a fired route carries the intent + the ported deterministic defaults (reference §3.1)
# --------------------------------------------------------------------------- #
def test_fired_route_carries_ported_defaults() -> None:
    d = deterministic_route("review this nda")
    assert d is not None
    assert d == Classification(intent="review", deterministic=True)
    # Every routing parameter at its deterministic default.
    assert d.jurisdiction == ""
    assert d.counterparty_type == ""
    assert d.mutuality == ""
    assert d.signer_emails == ()
    assert d.sequential is False
    assert d.cc_emails == ()
    assert d.cc_timing == "after"
    assert d.deterministic is True


def test_to_dict_shape_matches_reference_classified_payload() -> None:
    d = deterministic_route("archive")
    assert d is not None
    payload = d.to_dict()
    assert payload == {
        "intent": "archive",
        "jurisdiction": "",
        "counterparty_type": "",
        "mutuality": "",
        "signer_emails": [],  # lists, not tuples (reference §2.1 shape)
        "sequential": False,
        "cc_emails": [],
        "cc_timing": "after",
        "reasoning": "",
    }
    assert isinstance(payload["signer_emails"], list)


# --------------------------------------------------------------------------- #
# Normalization (reference §3.1: lowercase, strip apostrophes/punctuation, pleasantries, fillers)
# --------------------------------------------------------------------------- #
def test_normalize_lowercases_and_strips_punctuation() -> None:
    assert normalize("Review THIS, please!!!") == "review this please"


def test_normalize_strips_apostrophes() -> None:
    # "don't" → "dont", "I'd" → "id" (matches the apostrophe-less lead phrases).
    assert normalize("don't") == "dont"
    assert normalize("I’d like to review") == "id like to review"


def test_apostrophe_contraction_still_fires() -> None:
    # "don't review" normalizes to "dont review"; the review keyword still matches → review.
    d = deterministic_route("don't review this")
    assert d is not None and d.intent == "review"


def test_leading_pleasantries_and_trailing_fillers_stripped() -> None:
    # "can you ... for me" wrappers peel off; the lone review keyword fires.
    d = deterministic_route(
        "Hey there, could you please review the attached NDA for me? Thanks!"
    )
    assert d is not None and d.intent == "review"


def test_greeting_is_not_reduced_to_empty_then_misrouted() -> None:
    # A bare greeting is caught as a greeting (→ help) BEFORE pleasantry-stripping would blank it.
    for g in ("hi", "hello there", "good afternoon"):
        d = deterministic_route(g)
        assert d is not None and d.intent == "help"
