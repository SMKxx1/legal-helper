"""LLM intent classifier: the portable schema, the FakeAdapter round-trip, and the hardening matrix.

Zero network — every LLM call rides a fake adapter (``Gateway(FakeAdapter(...))``). The point of these
tests is the trust boundary (PLAN §3.3, reference §6): a HOSTILE or confused completion is CLAMPED to a
safe, in-bounds :class:`Classification`, never propagated. The classifier is only ever consulted when
the deterministic router defers, so parity with the ported ``Classified`` Set node is what matters.
"""

from __future__ import annotations

import json

import pytest

from app.ai.gateway import (
    Gateway,
    RawResult,
    SchemaValidationError,
    TerminalProviderError,
    Usage,
)
from app.bot.classifier import (
    CLASSIFIER_SCHEMA_V1,
    build_classifier_gateway,
    classify,
    harden,
)
from app.bot.router import Classification
from app.config import Settings
from app.engine.portable_schema import (
    assert_portable,
    assert_reasoning_before_verdict,
)


# --------------------------------------------------------------------------- #
# Fake adapter — a canned completion, zero network
# --------------------------------------------------------------------------- #
class FakeAdapter:
    """A ``ProviderAdapter`` that returns a canned JSON body (or raises a canned provider error)."""

    name = "fake"
    model_id = "fake/classifier"

    def __init__(self, obj: object = None, *, raises: Exception | None = None) -> None:
        self._text = obj if isinstance(obj, str) else json.dumps(obj)
        self._raises = raises

    def complete(self, req) -> RawResult:  # noqa: ANN001 - GatewayRequest
        if self._raises is not None:
            raise self._raises
        return RawResult(text=self._text, usage=Usage(), model_version=self.model_id)


def _gateway(obj: object = None, *, raises: Exception | None = None) -> Gateway:
    return Gateway(FakeAdapter(obj, raises=raises))


def _full(**overrides: object) -> dict:
    """A schema-complete classifier completion (all required keys present) with overrides applied."""
    base: dict = {
        "reasoning": "because",
        "intent": "review",
        "jurisdiction": None,
        "counterparty_type": None,
        "mutuality": None,
        "signer_emails": [],
        "sequential": False,
        "cc_emails": [],
        "cc_timing": "after",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Schema contract
# --------------------------------------------------------------------------- #
def test_classifier_schema_is_portable_and_reasoning_first() -> None:
    assert assert_portable(CLASSIFIER_SCHEMA_V1)
    # D1: the rationale decodes before the verdict (intent).
    assert assert_reasoning_before_verdict(
        CLASSIFIER_SCHEMA_V1, ["reasoning"], ["intent"]
    )


# --------------------------------------------------------------------------- #
# Hardening matrix (reference §6) — hostile / out-of-set LLM outputs are clamped
# --------------------------------------------------------------------------- #
def test_harden_intent_out_of_set_becomes_unknown() -> None:
    assert harden(_full(intent="delete_everything"), "x").intent == "unknown"
    assert harden(_full(intent=""), "x").intent == "unknown"
    assert harden(_full(intent="REVIEW"), "x").intent == "review"  # case-normalized


@pytest.mark.parametrize(
    "intent", ["template", "generate", "envelope", "review", "help", "archive"]
)
def test_harden_keeps_valid_intents(intent: str) -> None:
    assert harden(_full(intent=intent), "x").intent == intent


def test_harden_jurisdiction_clamped_to_us_sg() -> None:
    assert harden(_full(jurisdiction="us"), "x").jurisdiction == "US"
    assert harden(_full(jurisdiction="SG"), "x").jurisdiction == "SG"
    assert harden(_full(jurisdiction="france"), "x").jurisdiction == ""
    assert (
        harden(_full(jurisdiction="United States"), "x").jurisdiction == ""
    )  # not exact
    assert harden(_full(jurisdiction=None), "x").jurisdiction == ""


def test_harden_counterparty_clamped_and_normalized() -> None:
    assert (
        harden(_full(counterparty_type="company"), "x").counterparty_type == "company"
    )
    # spaces → underscores, lowercased.
    assert (
        harden(_full(counterparty_type="Service Provider"), "x").counterparty_type
        == "service_provider"
    )
    assert (
        harden(_full(counterparty_type="individual"), "x").counterparty_type
        == "individual"
    )
    assert harden(_full(counterparty_type="megacorp"), "x").counterparty_type == ""
    assert harden(_full(counterparty_type=None), "x").counterparty_type == ""


def test_harden_mutuality_is_strict_directionality_only() -> None:
    # Kept only when a directionality word is literally in the message AND the value is valid.
    assert (
        harden(_full(mutuality="mutual"), "we want a mutual nda").mutuality == "mutual"
    )
    assert (
        harden(_full(mutuality="unilateral"), "make it one-way").mutuality
        == "unilateral"
    )
    # No directionality word in the text → forced to "" even though the model asserted a value.
    assert harden(_full(mutuality="mutual"), "an nda for acme corp").mutuality == ""
    assert (
        harden(_full(mutuality="unilateral"), "please generate an nda").mutuality == ""
    )
    # Directionality word present but the model value is invalid → "".
    assert harden(_full(mutuality="sideways"), "a mutual nda").mutuality == ""


def test_harden_mutuality_never_inferred_from_counterparty_or_jurisdiction() -> None:
    # An individual + US company context must NOT invent mutuality without a directionality keyword.
    c = harden(
        _full(counterparty_type="individual", jurisdiction="US", mutuality="mutual"),
        "an NDA with an individual in the US",
    )
    assert c.mutuality == ""


@pytest.mark.parametrize(
    "phrase,expect",
    [
        ("mutual", "mutual"),
        ("mutually", "mutual"),
        ("bilateral", "mutual"),
        ("reciprocal", "mutual"),
        ("two-way", "mutual"),
        ("two way", "mutual"),
        ("unilateral", "unilateral"),
        ("one-way", "unilateral"),
        ("one way", "unilateral"),
        ("one-sided", "unilateral"),
    ],
)
def test_harden_directionality_keyword_variants_unlock_value(
    phrase: str, expect: str
) -> None:
    # Whatever the keyword, the STRICT rule only gates on presence; the value (mutual/unilateral) is
    # what the model returned. We assert the value is preserved when a keyword is present.
    text = f"please make a {phrase} nda"
    assert harden(_full(mutuality=expect), text).mutuality == expect


def test_harden_cc_timing_defaults_after() -> None:
    assert harden(_full(cc_timing="before"), "x").cc_timing == "before"
    assert harden(_full(cc_timing="after"), "x").cc_timing == "after"
    assert harden(_full(cc_timing="whenever"), "x").cc_timing == "after"
    assert harden(_full(cc_timing=""), "x").cc_timing == "after"


def test_harden_sequential_coerced_to_bool() -> None:
    assert harden(_full(sequential=True), "x").sequential is True
    assert harden(_full(sequential=False), "x").sequential is False
    assert (
        harden(_full(sequential="yes"), "x").sequential is True
    )  # hostile string coerced
    assert harden(_full(sequential="false"), "x").sequential is False
    assert harden(_full(sequential=1), "x").sequential is True
    assert harden(_full(sequential="garbage"), "x").sequential is False


def test_harden_emails_validated_deduped_lowercased() -> None:
    c = harden(
        _full(
            signer_emails=["Jane@X.com", "not-an-email", "jane@x.com", "bob@y.com", 42],
            cc_emails="legal@x.com; junk, ops@x.com",
        ),
        "x",
    )
    # Invalid dropped, case-folded, de-duplicated, order preserved.
    assert c.signer_emails == ("jane@x.com", "bob@y.com")
    # A separator-joined STRING is split and validated too (reference §3.7 modal split).
    assert c.cc_emails == ("legal@x.com", "ops@x.com")


def test_harden_missing_keys_default_safely() -> None:
    # A near-empty object (Gateway guarantees required keys in prod, but harden must not crash).
    c = harden({}, "x")
    assert c == Classification(intent="unknown")


def test_harden_non_dict_input_is_safe() -> None:
    assert harden(None, "x").intent == "unknown"  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# classify() end-to-end through a fake gateway — the whole path clamps
# --------------------------------------------------------------------------- #
def test_classify_hostile_completion_is_clamped() -> None:
    hostile = _full(
        intent="rm -rf",
        jurisdiction="atlantis",
        counterparty_type="OVERLORD",
        mutuality="mutual",  # but the text has no directionality word
        cc_timing="eventually",
        sequential="totally",  # not a recognized truthy token → clamps to False
        signer_emails=["ok@x.com", "bad"],
    )
    c = classify(
        "classify this message with no direction words", gateway=_gateway(hostile)
    )
    assert c.intent == "unknown"
    assert c.jurisdiction == ""
    assert c.counterparty_type == ""
    assert c.mutuality == ""  # no directionality keyword in the text
    assert c.cc_timing == "after"
    assert c.sequential is False
    assert c.signer_emails == ("ok@x.com",)
    assert c.deterministic is False


def test_classify_valid_completion_passes_through() -> None:
    good = _full(
        intent="envelope",
        signer_emails=["a@x.com", "b@y.com"],
        cc_emails=["cc@x.com"],
        cc_timing="before",
        sequential=True,
        reasoning="two signers named",
    )
    c = classify(
        "send to a@x.com and b@y.com for signature, cc cc@x.com first",
        gateway=_gateway(good),
    )
    assert c.intent == "envelope"
    assert c.signer_emails == ("a@x.com", "b@y.com")
    assert c.cc_emails == ("cc@x.com",)
    assert c.cc_timing == "before"
    assert c.sequential is True
    assert c.reasoning == "two signers named"


def test_classify_propagates_provider_error() -> None:
    # A terminal provider failure must surface (dispatch turns it into the friendly error reply),
    # never be silently swallowed into a default classification.
    with pytest.raises(TerminalProviderError):
        classify("anything", gateway=_gateway(raises=TerminalProviderError("boom")))


def test_classify_missing_required_key_raises_schema_error() -> None:
    # Gateway validates required-key presence; a malformed completion is a terminal schema error.
    incomplete = _full()
    del incomplete["intent"]
    with pytest.raises(SchemaValidationError):
        classify("anything", gateway=_gateway(incomplete))


# --------------------------------------------------------------------------- #
# Gateway selection (no network — just which adapter is chosen from config)
# --------------------------------------------------------------------------- #
def test_build_classifier_gateway_none_when_no_provider() -> None:
    s = Settings(_env_file=None, openrouter_api_key="", anthropic_api_key="")
    assert build_classifier_gateway(s) is None


def test_build_classifier_gateway_prefers_openrouter() -> None:
    s = Settings(
        _env_file=None, openrouter_api_key="sk-or-test", anthropic_api_key="sk-ant-test"
    )
    gw = build_classifier_gateway(s)
    assert gw is not None
    assert gw.adapter.name == "openrouter"
    # The classifier rides the cheap `router` alias (default anthropic/claude-haiku-4-5).
    assert gw.adapter.model_id == s.openrouter_model_router


class _StubAnthropicAdapter:
    """An ``AnthropicAdapter`` stand-in that records its model id without touching the real SDK."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str, cache_ttl: str = "5m") -> None:
        self.model_id = model


def test_build_classifier_gateway_anthropic_fallback_pins_haiku(monkeypatch) -> None:
    # Direct-Anthropic fallback: the classifier is the cheapest tier → Haiku, not the review model.
    s = Settings(
        _env_file=None,
        openrouter_api_key="",
        anthropic_api_key="sk-ant-test",
        anthropic_model="claude-opus-4-8",
    )

    import app.ai.adapters as adapters

    monkeypatch.setattr(adapters, "AnthropicAdapter", _StubAnthropicAdapter)
    gw = build_classifier_gateway(s)
    assert gw is not None
    assert gw.adapter.model_id == "claude-haiku-4-5"
