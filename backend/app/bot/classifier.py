"""LLM intent classifier + the fail-closed hardening layer (PLAN §3.3, reference §3.1/§6).

Invoked ONLY when :func:`app.bot.router.deterministic_route` defers (the message wasn't an
unambiguous bare command). It runs the ported few-shot, single-label classifier through the engine's
:class:`~app.ai.gateway.Gateway` on the cheap tier (the ``classifier`` alias — OpenRouter primary,
default ``anthropic/claude-haiku-4-5``; the direct-Anthropic Haiku adapter is the config fallback),
then HARDENS every field against the ported allowlists.

The two-stage design is the whole point (PLAN §3.3): **the LLM's output is NEVER trusted directly.**
The model proposes; :func:`harden` disposes — clamping intent to the known set (else ``unknown``),
jurisdiction to ``{US, SG}``, counterparty to ``{company, service_provider, individual}``, mutuality
to a literal directionality keyword actually present in the text (never inferred), ``cc_timing`` to
``{before, after}`` (default ``after``), ``sequential`` to a real bool, and email lists to
syntactically-valid addresses. So even a hostile or confused completion can only ever produce a safe,
in-bounds :class:`~app.bot.router.Classification`.

The structured-output schema is deliberately LENIENT (plain strings, no enums): the hardening layer is
the single source of truth for valid values, and a lenient schema guarantees the adapter's client-side
validator never turns a weird-but-parseable completion into a terminal error instead of letting it
reach the clamp. Reasoning decodes before the verdict (D1) so the rationale isn't post-hoc.

Tests inject a fake adapter (``Gateway(FakeAdapter(...))``) — zero network.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.ai.gateway import Gateway, GatewayRequest
from app.engine.portable_schema import (
    assert_portable,
    assert_reasoning_before_verdict,
)

from .router import INTENTS, Classification

if TYPE_CHECKING:
    from app.config import Settings

# --------------------------------------------------------------------------- #
# Structured-output schema (portable; reasoning-before-verdict = D1)
# --------------------------------------------------------------------------- #
#: The classifier's output contract. LENIENT by design (see the module docstring): ``intent`` /
#: ``jurisdiction`` / ``counterparty_type`` / ``mutuality`` / ``cc_timing`` are plain (nullable)
#: strings, NOT enums — :func:`harden` enforces membership. ``reasoning`` is first so the verdict
#: (``intent``) is decoded after the rationale under grammar-constrained decoding (D1).
CLASSIFIER_SCHEMA_V1: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "reasoning",
        "intent",
        "jurisdiction",
        "counterparty_type",
        "mutuality",
        "signer_emails",
        "sequential",
        "cc_emails",
        "cc_timing",
    ],
    "properties": {
        "reasoning": {"type": "string"},
        "intent": {"type": "string"},
        "jurisdiction": {"type": ["string", "null"]},
        "counterparty_type": {"type": ["string", "null"]},
        "mutuality": {"type": ["string", "null"]},
        "signer_emails": {"type": "array", "items": {"type": "string"}},
        "sequential": {"type": "boolean"},
        "cc_emails": {"type": "array", "items": {"type": "string"}},
        "cc_timing": {"type": "string"},
    },
}

# Fail fast at import if the schema drifts out of portability / D1 ordering.
assert assert_portable(CLASSIFIER_SCHEMA_V1)
assert assert_reasoning_before_verdict(CLASSIFIER_SCHEMA_V1, ["reasoning"], ["intent"])

#: The request role — also the OpenRouter ``response_format.json_schema.name`` derivative + metrics key.
CLASSIFIER_ROLE = "bot_intent_classifier"

# --------------------------------------------------------------------------- #
# System prompt — the ported classifier contract (reference §3.1 LLM section)
# --------------------------------------------------------------------------- #
CLASSIFIER_SYSTEM = """\
You are the intent classifier for Amperesand's NDA assistant. Classify ONE inbound message into \
exactly one intent and extract routing parameters. Reply with ONLY a single JSON object matching the \
required schema — no prose, no markdown, no code fences. Put your one-sentence `reasoning` first, \
then the verdict.

Intents (choose exactly one):
- template  : the USER wants a blank/empty NDA document to fill in themselves (a template, a blank, \
a sample, a copy). WHO FILLS THE BLANKS decides template vs generate: user-fills = template.
- generate  : the BOT should fill in a finished NDA for the user. bot-fills = generate. A bare, \
unqualified request for "an NDA" with no other signal DEFAULTS TO GENERATE.
- review    : the user wants feedback / an automated review of an existing NDA (usually attached).
- envelope  : the user wants to send an NDA out for signature via DocuSign (sign, signature, signers, \
execute, for signing).
- archive   : file / save a SIGNED NDA into the archive.
- help      : the user asks what the assistant can do or how to use it, or just greets.
- unknown   : none of the above.

Key rules:
- template vs generate: a blank/format NOUN ("template", "blank", "copy", "sample", "form") BEATS an \
action VERB ("generate", "create", "make", "draft"). "make me a copy of the NDA" = template; \
"make me an NDA" = generate.
- Multi-step message: classify by the FIRST required action ("review this then send for signature" = \
review).

Parameter extraction (omit / null when not explicitly present — NEVER guess):
- jurisdiction: "US" or "SG" only, if explicitly stated (e.g. "US", "United States" -> US; "SG", \
"Singapore" -> SG); else null.
- counterparty_type: "company", "service_provider", or "individual", if stated; else null.
- mutuality: "mutual" or "unilateral" ONLY if the message literally uses a directionality word \
(mutual, mutually, bilateral, reciprocal, two-way, unilateral, one-way, one-sided). NEVER infer \
mutuality from the counterparty type or the jurisdiction. Else null.
- signer_emails: email addresses named as signers (order preserved); else [].
- cc_emails: email addresses named to be CC'd; else [].
- sequential: true ONLY if the message asks for ordered / sequential / one-at-a-time signing; else \
false.
- cc_timing: "before" or "after" (whether CCs receive the envelope before or after signing); \
default "after".
- reasoning: one short sentence naming the decisive signal.

Worked examples (message -> intent):
- "send me the US company NDA template" -> template (blank/noun; jurisdiction US, counterparty company)
- "can I get a blank mutual NDA for an individual" -> template (blank; individual; mutuality mutual)
- "make me a copy of our standard NDA" -> template (copy = user fills)
- "generate an NDA for Acme Corp" -> generate (bot fills; bare-ish request)
- "I need an NDA" -> generate (bare request defaults to generate)
- "draft a one-way NDA with a service provider" -> generate (mutuality unilateral; service_provider)
- "review this NDA" -> review
- "please look over the attached agreement and give feedback" -> review
- "send this to DocuSign for jane@x.com and bob@y.com to sign" -> envelope (signer_emails)
- "get these signed sequentially, CC legal@x.com after" -> envelope (sequential true; cc_timing after)
- "archive this signed NDA" -> archive
- "what can you do?" -> help
- "hey there" -> help
- "what's the weather" -> unknown
"""


# --------------------------------------------------------------------------- #
# Hardening — the ported `Classified` Set node (reference §6). LLM output is NEVER trusted.
# --------------------------------------------------------------------------- #
_VALID_JURISDICTIONS = frozenset({"US", "SG"})
_VALID_COUNTERPARTIES = frozenset({"company", "service_provider", "individual"})
_VALID_MUTUALITY = frozenset({"mutual", "unilateral"})
_VALID_CC_TIMING = frozenset({"before", "after"})

#: STRICT mutuality gate (reference §6): a directionality word must literally appear in the message,
#: else mutuality is forced to "" no matter what the model said. Hyphen or space variants accepted.
_DIRECTIONALITY_RE = re.compile(
    r"\b(?:mutual|mutually|bilateral|reciprocal|two[-\s]?way|"
    r"unilateral|one[-\s]?way|one[-\s]?sided)\b",
    re.IGNORECASE,
)

#: A pragmatic, injection-safe email shape (no spaces, one @, a dotted domain). We validate — never
#: trust — every address the model hands back.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: How the modal split CC lists (reference §3.7): newline / comma / semicolon / whitespace.
_EMAIL_SPLIT_RE = re.compile(r"[\s,;]+")


def _as_str(v: object) -> str:
    return v.strip() if isinstance(v, str) else ""


def _as_bool(v: object) -> bool:
    """Coerce a possibly-hostile ``sequential`` to a real bool (a non-strict model may emit a string)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in {"true", "yes", "y", "1", "sequential", "ordered"}
    return False


def _clean_emails(v: object) -> tuple[str, ...]:
    """Validate + dedupe email addresses from a list (or a separator-joined string). Invalid dropped."""
    if isinstance(v, str):
        candidates: list[object] = _EMAIL_SPLIT_RE.split(v)
    elif isinstance(v, (list, tuple)):
        candidates = list(v)
    else:
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        if not isinstance(c, str):
            continue
        e = c.strip().strip("<>").lower()
        if e and _EMAIL_RE.match(e) and e not in seen:
            seen.add(e)
            out.append(e)
    return tuple(out)


def harden(obj: dict, text: str) -> Classification:
    """Clamp a raw classifier completion into a safe :class:`Classification` (reference §6).

    ``text`` is the ORIGINAL message body — required for the strict mutuality directionality check.
    Every rule fails CLOSED: an out-of-set / missing / malformed value collapses to its safe default
    rather than propagating the model's assertion.
    """
    obj = obj if isinstance(obj, dict) else {}

    # intent: allowlist else unknown.
    intent = _as_str(obj.get("intent")).lower()
    if intent not in INTENTS:
        intent = "unknown"

    # jurisdiction: {US, SG} (uppercased) else "".
    jurisdiction = _as_str(obj.get("jurisdiction")).upper()
    if jurisdiction not in _VALID_JURISDICTIONS:
        jurisdiction = ""

    # counterparty_type: {company, service_provider, individual}; lowercase, spaces -> underscores.
    counterparty = _as_str(obj.get("counterparty_type")).lower().replace(" ", "_")
    if counterparty not in _VALID_COUNTERPARTIES:
        counterparty = ""

    # mutuality: STRICT — only kept when a directionality word is literally in the message AND the
    # model's value is a valid literal. NEVER inferred from counterparty / jurisdiction.
    mutuality = _as_str(obj.get("mutuality")).lower()
    if not (_DIRECTIONALITY_RE.search(text or "") and mutuality in _VALID_MUTUALITY):
        mutuality = ""

    # cc_timing: {before, after}, default after.
    cc_timing = _as_str(obj.get("cc_timing")).lower()
    if cc_timing not in _VALID_CC_TIMING:
        cc_timing = "after"

    return Classification(
        intent=intent,
        jurisdiction=jurisdiction,
        counterparty_type=counterparty,
        mutuality=mutuality,
        signer_emails=_clean_emails(obj.get("signer_emails")),
        sequential=_as_bool(obj.get("sequential")),
        cc_emails=_clean_emails(obj.get("cc_emails")),
        cc_timing=cc_timing,
        reasoning=_as_str(obj.get("reasoning")),
        deterministic=False,
    )


# --------------------------------------------------------------------------- #
# The classifier
# --------------------------------------------------------------------------- #
def _build_request(text: str) -> GatewayRequest:
    return GatewayRequest(
        role=CLASSIFIER_ROLE,
        schema=CLASSIFIER_SCHEMA_V1,
        system=CLASSIFIER_SYSTEM,
        task=text or "",
        stable_blocks=[],
        effort="low",  # cheap tier; ignored by Haiku (reasoning omitted), harmless elsewhere
        max_tokens=512,
    )


def classify(text: str, *, gateway: Gateway) -> Classification:
    """Classify ``text`` via the LLM, then harden the result.

    Raises the gateway's provider error (``RetryableProviderError`` / ``TerminalProviderError`` /
    ``SchemaValidationError``) on an LLM failure — the dispatcher catches it and delivers the ported
    "sorry, I hit a problem" reply, mirroring the n8n classifier's ``onError -> Flow Error Reply``.
    No fallback is passed: a classifier failure must be observable, not silently routed to help.
    """
    result = gateway.run(_build_request(text))
    return harden(result.obj, text)


def build_classifier_gateway(settings: Settings) -> Gateway | None:
    """Construct the cheap-tier classifier gateway from config, or ``None`` when no LLM is configured.

    OpenRouter is the ZDR-pinned primary (the ``classifier`` alias = ``openrouter_model_router``,
    default ``anthropic/claude-haiku-4-5``); the direct-Anthropic Haiku adapter is the config
    fallback (reference §3.1: ``claude-haiku-4-5``, the cheapest tier). ``None`` means the classifier
    can't run — the dispatcher then degrades a deferred message to ``unknown`` (→ help) rather than
    erroring.
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

        # The classifier is the cheapest tier — pin Haiku regardless of the review model (reference
        # §3.1: claude-haiku-4-5), not settings.anthropic_model (which is the review default).
        return Gateway(AnthropicAdapter(settings.anthropic_api_key, "claude-haiku-4-5"))
    return None
