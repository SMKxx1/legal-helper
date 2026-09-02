"""Signed-NDA classifier for the cache-folder watcher (PLAN §3.10, reference §3.11).

Ports the n8n "Classify NDA" node (reference §3.11): a legal-document classifier that reads a signed
NDA's text and returns the issuer, recipient, mutuality (``mNDA``/``uNDA``), counterparty name, and
effective date — the fields the watcher composes into the ported auto-name
``<yyyyMMdd>_<issuer>_<mNDA|uNDA>_<recipient>.pdf``.

Two deliberate changes from the n8n original (both PLAN-directed):

* **Provider** — the old node used OpenAI ``gpt-5.5`` (Responses API). The rebuild routes through the
  engine's :class:`~app.ai.gateway.Gateway` on the CHEAP alias (``openrouter_model_router``, default
  ``anthropic/claude-haiku-4-5``; the direct-Anthropic Haiku adapter is the config fallback) — the same
  cheap tier the bot intent classifier uses (PLAN §3.8 aliases; this is name-extraction, not review).
* **Input** — the old node attached the PDF as base64 for a vision model. The cheap text alias is fed
  the EXTRACTED text instead; the watcher supplies it. When too little text is extractable the classify
  simply fails/returns incomplete fields → the watcher's ported ``saved_default_name`` fallback.

As with the bot intent classifier, the model's output is NEVER trusted directly: :func:`harden` clamps
``nda_type`` to ``{mNDA, uNDA}``, cleans the party names, and validates the effective date to
``yyyyMMdd`` (else blank). The portable schema is lenient (plain strings) so a weird-but-parseable
completion reaches the clamp rather than becoming a terminal schema error. Reasoning decodes before the
verdict (D1). Tests inject a fake adapter (``Gateway(FakeAdapter(...))``) — zero network.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.ai.gateway import Gateway, GatewayRequest
from app.engine.portable_schema import assert_portable, assert_reasoning_before_verdict

from .naming import NDA_TYPE_MUTUAL, NDA_TYPE_UNILATERAL, clean_party

if TYPE_CHECKING:
    from app.config import Settings

# --------------------------------------------------------------------------- #
# Structured-output schema (portable; reasoning-before-verdict = D1)
# --------------------------------------------------------------------------- #
#: The classifier's output contract. LENIENT by design (plain nullable strings, no enums): :func:`harden`
#: is the single source of truth for valid values. ``reasoning`` is first so the verdict fields decode
#: after the rationale under grammar-constrained decoding (D1).
CACHE_CLASSIFY_SCHEMA_V1: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "reasoning",
        "issuer",
        "recipient",
        "nda_type",
        "counterparty_name",
        "effective_date",
    ],
    "properties": {
        "reasoning": {"type": "string"},
        "issuer": {"type": ["string", "null"]},
        "recipient": {"type": ["string", "null"]},
        "nda_type": {"type": ["string", "null"]},
        "counterparty_name": {"type": ["string", "null"]},
        "effective_date": {"type": ["string", "null"]},
    },
}

assert assert_portable(CACHE_CLASSIFY_SCHEMA_V1)
assert assert_reasoning_before_verdict(
    CACHE_CLASSIFY_SCHEMA_V1, ["reasoning"], ["issuer", "recipient", "nda_type"]
)

#: The request role — also the OpenRouter ``response_format.json_schema.name`` derivative + metrics key.
CACHE_CLASSIFY_ROLE = "archive_nda_classifier"

# --------------------------------------------------------------------------- #
# System prompt — the ported classifier contract (reference §3.11)
# --------------------------------------------------------------------------- #
CACHE_CLASSIFY_SYSTEM = """\
You are a legal-document classifier for Amperesand. You are given the text of ONE fully-signed NDA. \
Extract the fields below and reply with ONLY a single JSON object matching the required schema — no \
prose, no markdown, no code fences. Put your one-sentence `reasoning` first, then the fields.

Exactly one party to this NDA is an Amperesand entity; the other is the counterparty.

Fields:
- issuer: the disclosing party / sender — the party's FULL legal name (include entity suffixes like \
Inc, LLC, Pte Ltd, Ltd), but remove commas and periods (keep & ( ) -).
- recipient: the receiving party / counterparty — its FULL legal name, same formatting rule.
- nda_type: EXACTLY "mNDA" if the agreement is mutual/bilateral, or "uNDA" if it is one-way/unilateral. \
No other value.
- counterparty_name: the non-Amperesand party's full legal name (same as recipient when Amperesand is \
the issuer; otherwise the issuer).
- effective_date: the effective date as yyyyMMdd. Use the date stated in the agreement; if none is \
stated use the date of the LAST signature; if neither is present output an empty string.
- reasoning: one short sentence naming the decisive signal (which party is Amperesand, mutual vs one-way).
"""

# --------------------------------------------------------------------------- #
# Hardening — the model's output is NEVER trusted directly
# --------------------------------------------------------------------------- #
_YYYYMMDD_RE = re.compile(r"^\d{8}$")
_VALID_NDA_TYPES = frozenset({NDA_TYPE_MUTUAL, NDA_TYPE_UNILATERAL})
_NDA_TYPE_ALIASES = {
    "mnda": NDA_TYPE_MUTUAL,
    "mutual": NDA_TYPE_MUTUAL,
    "bilateral": NDA_TYPE_MUTUAL,
    "two-way": NDA_TYPE_MUTUAL,
    "unda": NDA_TYPE_UNILATERAL,
    "unilateral": NDA_TYPE_UNILATERAL,
    "one-way": NDA_TYPE_UNILATERAL,
    "one-sided": NDA_TYPE_UNILATERAL,
}


@dataclass(frozen=True)
class CacheClassification:
    """A hardened classification of one signed NDA (reference §3.11).

    ``is_complete`` is the ported "namingFailed" guard, inverted: an auto-name is only produced when the
    issuer, recipient, and a valid ``nda_type`` are ALL present — otherwise the watcher files the file
    under its original name with status ``saved_default_name``.
    """

    issuer: str
    recipient: str
    nda_type: str
    counterparty_name: str = ""
    effective_date: str = ""
    reasoning: str = ""

    @property
    def is_complete(self) -> bool:
        """True iff issuer + recipient + a valid ``mNDA``/``uNDA`` were all extracted (rename-eligible)."""
        return bool(
            self.issuer and self.recipient and self.nda_type in _VALID_NDA_TYPES
        )


def _as_str(v: object) -> str:
    return v.strip() if isinstance(v, str) else ""


def _harden_nda_type(v: object) -> str:
    """Clamp ``nda_type`` to exactly ``mNDA`` / ``uNDA``. An exact-case hit wins; else map a known
    synonym; else blank (→ the ported ``saved_default_name`` fallback)."""
    raw = _as_str(v)
    if raw in _VALID_NDA_TYPES:
        return raw
    return _NDA_TYPE_ALIASES.get(raw.lower(), "")


def harden(obj: dict) -> CacheClassification:
    """Clamp a raw classifier completion into a safe :class:`CacheClassification` (reference §3.11).

    Party names are run through :func:`app.archive.naming.clean_party` (drop commas/periods, keep
    ``& ( ) -``) so the extraction and the eventual filename agree; ``nda_type`` is clamped to the two
    valid codes; ``effective_date`` is kept only when it is a literal ``yyyyMMdd`` (else blank → the
    watcher falls back to today's date). Every field fails to a safe empty default rather than
    propagating the model's assertion.
    """
    obj = obj if isinstance(obj, dict) else {}
    effective = _as_str(obj.get("effective_date"))
    return CacheClassification(
        issuer=clean_party(_as_str(obj.get("issuer"))),
        recipient=clean_party(_as_str(obj.get("recipient"))),
        nda_type=_harden_nda_type(obj.get("nda_type")),
        counterparty_name=clean_party(_as_str(obj.get("counterparty_name"))),
        effective_date=effective if _YYYYMMDD_RE.match(effective) else "",
        reasoning=_as_str(obj.get("reasoning")),
    )


# --------------------------------------------------------------------------- #
# The classifier
# --------------------------------------------------------------------------- #
#: The watcher's classify seam: ``(text) -> CacheClassification``. Injected in tests (a stub — no
#: gateway), resolved to :func:`classify_nda` over a cheap gateway in production. Raising / returning an
#: incomplete result both route to the ported ``saved_default_name`` fallback in the watcher.
Classifier = Callable[[str], "CacheClassification"]


def _build_request(text: str) -> GatewayRequest:
    return GatewayRequest(
        role=CACHE_CLASSIFY_ROLE,
        schema=CACHE_CLASSIFY_SCHEMA_V1,
        system=CACHE_CLASSIFY_SYSTEM,
        task=text or "",
        stable_blocks=[],
        effort="low",  # cheap tier; ignored by Haiku, harmless elsewhere
        max_tokens=512,
    )


def classify_nda(text: str, *, gateway: Gateway) -> CacheClassification:
    """Classify a signed NDA's ``text`` via the cheap LLM alias, then harden the result.

    Raises the gateway's provider error (``RetryableProviderError`` / ``TerminalProviderError`` /
    ``SchemaValidationError``) on an LLM failure — the watcher catches it and files the file under its
    original name (``saved_default_name``), mirroring the n8n classifier's ``onError → namingFailed``
    path. No fallback is passed to the gateway: a classify failure must be observable, not silently
    routed to a wrong name."""
    result = gateway.run(_build_request(text))
    return harden(result.obj)


def build_classify_gateway(settings: Settings) -> Gateway | None:
    """Construct the cheap-tier classify gateway from config, or ``None`` when no LLM is configured.

    OpenRouter is the ZDR-pinned primary (the cheap ``router`` alias = ``openrouter_model_router``,
    default ``anthropic/claude-haiku-4-5``); the direct-Anthropic Haiku adapter is the config fallback
    (reference §3.11 used a cheap model). ``None`` means classification can't run — the watcher then
    files every file under its original name (``saved_default_name``) rather than erroring.
    """
    if settings.openrouter_api_key:
        from app.ai.openrouter import OpenRouterAdapter

        return Gateway(
            OpenRouterAdapter(
                settings.openrouter_api_key,
                settings.openrouter_model_router,
                base_url=settings.openrouter_base_url,
                zdr_only=bool(settings.openrouter_zdr_only),
                provider_only=settings.openrouter_provider_only_list,
                timeout_s=float(getattr(settings, "provider_timeout_s", 150.0)),
            )
        )
    if settings.anthropic_api_key:
        from app.ai.adapters import AnthropicAdapter

        return Gateway(AnthropicAdapter(settings.anthropic_api_key, "claude-haiku-4-5"))
    return None


def default_classifier(settings: Settings) -> Classifier | None:
    """A ready-to-call ``(text) -> CacheClassification`` bound to the cheap gateway, or ``None`` when no
    LLM is configured (the watcher then files everything under its default name)."""
    gateway = build_classify_gateway(settings)
    if gateway is None:
        return None

    def _classify(text: str) -> CacheClassification:
        return classify_nda(text, gateway=gateway)

    return _classify


__all__ = [
    "CacheClassification",
    "Classifier",
    "classify_nda",
    "harden",
    "build_classify_gateway",
    "default_classifier",
    "CACHE_CLASSIFY_SCHEMA_V1",
    "CACHE_CLASSIFY_SYSTEM",
    "CACHE_CLASSIFY_ROLE",
]
