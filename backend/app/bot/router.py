"""Deterministic keyword router (PLAN §3.3) — the fast, LLM-free first pass over an inbound message.

A verbatim behavioral port of the n8n Router's ``Deterministic Route`` Code node (ground-truth
reference §3.1, §6). Its job is deliberately narrow: short-circuit only the *truly unambiguous*
single-intent messages so the cheap-tier LLM classifier (``app.bot.classifier``) is spared the easy
cases, and DELIBERATELY defer everything ambiguous to it.

Contract (reference §6, authoritative):

* **Fires an intent** for bare single-intent commands — ``help`` / ``review`` / ``archive`` /
  ``generate`` — and for greetings (mapped to ``help``, the friendly onboarding surface). "Bare"
  means: after normalization the message carries exactly ONE deliverable keyword and none of the
  defer triggers below.
* **Defers to the classifier** (returns ``None`` — the n8n ``''`` sentinel) for:
    - **template** keywords (``template`` / ``blank`` / ``empty`` / ``sample`` / ``copy``): the
      picker needs jurisdiction + counterparty + (individual) mutuality, which only the LLM extracts;
    - **envelope** keywords (``docusign`` / ``sign`` / ``signature`` / ``signer`` / …): signer emails,
      ordering and CC timing are LLM-extracted;
    - any message carrying **≥2 deliverable keywords** (genuinely multi-intent / ambiguous);
    - any message with **no** recognized keyword.

Normalization mirrors the n8n node: lowercase, strip apostrophes, punctuation → spaces, collapse
whitespace, strip leading pleasantries ("hi", "can you", "please", …) and trailing fillers ("please",
"thanks", "for me", …). Greetings are detected on the punctuation-stripped text *before* pleasantries
are removed (so a bare "hi" is recognized as a greeting rather than reduced to nothing).

Output parity: a fired route yields a :class:`Classification` with the intent set and every routing
parameter at its deterministic default (``jurisdiction=''``, ``counterparty_type=''``,
``mutuality=''``, ``signer_emails=()``, ``sequential=False``, ``cc_emails=()``, ``cc_timing='after'``)
— exactly the field set the n8n node emitted with ``_det:true``. The classifier fills those in for
the deferred cases.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from ..telemetry import bind_correlation_id, correlation_id_var, get_logger
from .envelope import Envelope

if TYPE_CHECKING:
    from app.ai.gateway import Gateway
    from app.config import Settings

    from .intents import IntentRegistry, IntentReply

log = get_logger("nda.bot.router")

# --------------------------------------------------------------------------- #
# Classification — the shared routing contract (deterministic + classifier)
# --------------------------------------------------------------------------- #
#: The intents the whole system knows (reference §1 dispatch map + §6). ``unknown`` routes to help.
INTENTS: frozenset[str] = frozenset(
    {"template", "generate", "envelope", "review", "help", "archive", "unknown"}
)

#: Intents that are ALSO deliverable keywords the deterministic router can fire on directly. Note
#: ``template`` and ``envelope`` are intentionally NOT here — they always defer to the classifier.
_FIREABLE: tuple[str, ...] = ("help", "review", "archive", "generate")


@dataclass(frozen=True)
class Classification:
    """The normalized routing decision — the router's output and the classifier's output shape.

    Frozen + typed so a handler can never mutate the decision it was dispatched under, and so the
    deterministic path and the LLM path produce the identical contract (parity with the n8n
    ``classified`` item). Email lists are tuples (hashable/immutable); ``to_dict`` renders them back
    to lists for the n8n-shaped ``{intent, jurisdiction, …}`` payload.
    """

    intent: str
    jurisdiction: str = ""
    counterparty_type: str = ""
    mutuality: str = ""
    signer_emails: tuple[str, ...] = ()
    sequential: bool = False
    cc_emails: tuple[str, ...] = ()
    cc_timing: str = "after"
    #: The LLM's one-line rationale (empty on the deterministic path — no model was consulted).
    reasoning: str = ""
    #: True when produced by the deterministic router; False when produced by the LLM classifier.
    deterministic: bool = field(default=False)

    def to_dict(self) -> dict:
        """The n8n-shaped ``classified`` payload (reference §2.1) — lists, not tuples."""
        return {
            "intent": self.intent,
            "jurisdiction": self.jurisdiction,
            "counterparty_type": self.counterparty_type,
            "mutuality": self.mutuality,
            "signer_emails": list(self.signer_emails),
            "sequential": self.sequential,
            "cc_emails": list(self.cc_emails),
            "cc_timing": self.cc_timing,
            "reasoning": self.reasoning,
        }


def _fire(intent: str) -> Classification:
    """A deterministic route: the intent with every routing parameter at its ported default."""
    return Classification(intent=intent, deterministic=True)


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #
#: Leading pleasantries / politeness wrappers stripped before keyword matching. The alternation is
#: applied greedily from the start (``+``) so stacked openers ("hi there, can you please …") peel off.
_LEAD_PHRASES: tuple[str, ...] = (
    "hi",
    "hello",
    "hey",
    "yo",
    "hiya",
    "greetings",
    "good morning",
    "good afternoon",
    "good evening",
    "there",
    "team",
    "bot",
    "ok",
    "okay",
    "so",
    "well",
    "please",
    "pls",
    "plz",
    "kindly",
    "thanks",
    "thank you",
    "thx",
    "can you",
    "could you",
    "would you",
    "will you",
    "can i",
    "could i",
    "i want to",
    "i wanna",
    "i would like to",
    "i d like to",
    "id like to",
    "i need to",
    "i need",
    "i wish to",
    "lets",
    "let us",
    "let me",
)
_LEAD_RE = re.compile(r"^(?:(?:" + "|".join(_LEAD_PHRASES) + r")\b\s*)+")

#: Trailing fillers stripped after keyword matching context is set.
_TRAIL_PHRASES: tuple[str, ...] = (
    "please",
    "pls",
    "plz",
    "thanks",
    "thank you",
    "thx",
    "for me",
    "for us",
    "asap",
    "now",
    "today",
    "quickly",
    "real quick",
    "when you can",
    "if you can",
)
_TRAIL_RE = re.compile(r"(?:\s*\b(?:" + "|".join(_TRAIL_PHRASES) + r")\b)+$")

#: A message that is nothing but a greeting → short-circuit to help (reference §6). Matched on the
#: punctuation-stripped text BEFORE pleasantries are removed (a bare "hi" must not reduce to "").
_GREETING_RE = re.compile(
    r"^(?:hi|hello|hey|yo|hiya|sup|greetings|good morning|good afternoon|good evening)"
    r"(?:\s+(?:there|team|bot|all|everyone|folks))?$"
)


def normalize(text: str) -> str:
    """Lowercase, strip apostrophes, punctuation→space, collapse whitespace (the n8n first pass)."""
    t = (text or "").lower()
    # Unify curly/backtick apostrophes then DELETE them so "don't"→"dont", "I'd"→"id" (matches the
    # apostrophe-less lead phrases above). Do this before punctuation→space so contractions join.
    t = t.replace("’", "'").replace("`", "'").replace("'", "")
    t = re.sub(r"[^a-z0-9\s]+", " ", t)  # every other punctuation becomes a space
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _strip_wrappers(n0: str) -> str:
    """Peel leading pleasantries and trailing fillers off the normalized text."""
    return _TRAIL_RE.sub("", _LEAD_RE.sub("", n0)).strip()


# --------------------------------------------------------------------------- #
# Deliverable-keyword detection (word-boundary regexes)
# --------------------------------------------------------------------------- #
# Conservative on purpose: a false keyword hit at worst forces a (correct) LLM classification, but a
# spuriously-fired intent skips the LLM entirely. So bare "check"/"file"/"save"/"send" are excluded —
# only unambiguous deliverable verbs/nouns match.
_HELP_KW = re.compile(
    r"\b(help|commands?|instructions?|how do you|how does this|what can you do|"
    r"what do you do|usage)\b"
)
_REVIEW_KW = re.compile(
    r"\b(review|reviews|reviewed|reviewing|redline|redlines|red line|red lines|"
    r"feedback|look over|check over|vet)\b"
)
_ARCHIVE_KW = re.compile(
    r"\b(archive|archived|archiving|file away|filed away|save to (?:drive|the cache|cache)|"
    r"store this|store it)\b"
)
_GENERATE_KW = re.compile(
    r"\b(generate|create|make|draft|produce|prepare|fill in|fill out|new nda|"
    r"complete(?:d)? nda)\b"
)
_TEMPLATE_KW = re.compile(r"\b(template|templates|blank|empty|sample|copy)\b")
_ENVELOPE_KW = re.compile(
    r"\b(envelope|docusign|signature|signatures|signer|signers|sign|for signing|"
    r"send for signature|execute|counterpart(?:y|ies))\b"
)

#: (name, regex) in fire-precedence order. ``template``/``envelope`` handled separately (always defer).
_DELIVERABLE_KW: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("help", _HELP_KW),
    ("review", _REVIEW_KW),
    ("archive", _ARCHIVE_KW),
    ("generate", _GENERATE_KW),
)


# --------------------------------------------------------------------------- #
# The router
# --------------------------------------------------------------------------- #
def deterministic_route(text: str) -> Classification | None:
    """Route ``text`` deterministically, or return ``None`` to defer to the LLM classifier.

    Ported decision order (reference §3.1 / §6):

    1. empty message → ``help`` (friendly default; the has-content guard drops truly empty ones);
    2. pure greeting → ``help``;
    3. after stripping pleasantries, a message reduced to nothing → ``help``;
    4. any **template** or **envelope** keyword → defer (``None``);
    5. **≥2** deliverable keywords → defer (``None``);
    6. exactly **one** deliverable keyword → fire that intent;
    7. no recognized keyword → defer (``None``).
    """
    n0 = normalize(text)
    if not n0:
        return _fire("help")
    if _GREETING_RE.match(n0):
        return _fire("help")

    n = _strip_wrappers(n0)
    if not n:
        return _fire("help")

    # Manual expiration commands (P4): "set expiration of <file> to YYYY-MM-DD" /
    # "re-extract expiration of <file>" are precise imperative shapes — matched BEFORE the keyword
    # pass so their file references can't trip the deliverable-keyword heuristics. The intent module
    # owns the grammar (single source: matches_expiration_command / parse_expiration_command).
    from .intents.expiration import matches_expiration_command

    if matches_expiration_command(text):
        return _fire("expiration")

    # Template + envelope ALWAYS defer: their routing parameters are the classifier's job.
    if _TEMPLATE_KW.search(n) or _ENVELOPE_KW.search(n):
        return None

    present = [name for name, rx in _DELIVERABLE_KW if rx.search(n)]
    if len(present) >= 2:
        return None  # multi-intent / ambiguous — let the classifier disambiguate
    if len(present) == 1:
        return _fire(present[0])
    return None  # no deliverable keyword — defer to the classifier


# =========================================================================== #
# The routing pipeline: route -> gate -> dispatch -> reply (PLAN §3.3 / §3.4)
# =========================================================================== #
# ``app.bot.dispatch`` (the intake seam: has-content guard, fail-closed dedup, durable claim, crash
# recovery — owned by the worker/email agent) calls ``route_envelope(envelope)`` per its documented
# contract. So the ported Router's *routing + dispatch* half lands HERE, beside the deterministic
# router it begins with. Everything below is fail-soft: a routing/classifier/handler failure degrades
# to the ported friendly-error reply (reference §2.7), never crashing the channel's turn.

#: Intents gated by the allowlist / approvals flow (PLAN §3.4, reference §6). The gate HOOK decides.
GATED_INTENTS: frozenset[str] = frozenset({"review", "envelope"})

#: The ported friendly error reply (reference §2.7 "Flow Error Reply", verbatim mrkdwn).
ERROR_REPLY_TEXT = (
    "*Sorry — I hit a problem finishing that request.* Please try again in a moment. "
    "If it keeps happening, let the team know."
)


# --------------------------------------------------------------------------- #
# Allowlist / approvals gate (HOOK — real implementation is wave B)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GateDecision:
    """A gate's verdict for one (envelope, intent):

    * ``allowed`` — proceed.
    * ``needs_confirmation`` — the user isn't exempt; a request row exists as ``awaiting_confirmation``
      (with the doc/origin stashed). Slack asks the user to confirm before pinging admins; email
      auto-advances to requesting (no buttons).
    * ``pending`` — already awaiting an admin decision (a re-ask, or an error fail-closed).
    """

    status: Literal["allowed", "needs_confirmation", "pending"]
    reason: str = ""
    #: The idempotent pending-request handle (the ported ``req_<md5(sender+intent)>`` shape) — set on
    #: a ``pending``/``needs_confirmation`` decision so the reply can quote it. Empty when allowed.
    request_key: str = ""
    #: Display names of the admins who can approve (shown in the confirmation prompt).
    admin_names: tuple[str, ...] = ()


@runtime_checkable
class ApprovalGate(Protocol):
    """The allowlist/approvals seam. Wave B swaps in the real ``nda_allowlist`` / ``nda_pending_requests``
    gate keyed on the envelope's VERIFIED identity (PLAN §3.4); the pipeline depends only on ``check``."""

    def check(self, envelope: Envelope, classification: Classification) -> GateDecision:
        """Decide whether ``classification.intent`` may run for ``envelope``."""
        ...


class AllowAllGate:
    """An explicit allow-EVERYTHING gate — the deliberately-open override, NOT the default.

    :func:`process` now defaults to the real fail-CLOSED allowlist/approvals gate
    (:class:`app.bot.approvals.AllowlistGate`, PLAN §3.4). This gate is kept as a named, injectable
    escape hatch: a unit test exercising dispatch/delivery mechanics on a gated intent, or a deployment
    that has intentionally disabled approvals, can opt out of gating *explicitly* rather than by
    accident. It touches no DB and allows every intent.
    """

    def check(self, envelope: Envelope, classification: Classification) -> GateDecision:
        return GateDecision(status="allowed")


def _default_gate(
    *,
    settings: Settings | None,
    service: _ReplyDeliverer | None,
    post_blocks: PostBlocks | None,
) -> ApprovalGate:
    """Build the production allowlist/approvals gate (PLAN §3.4), wired to announce allowlist misses to
    the admin through the SAME delivery the pipeline replies on (Slack Block Kit → ``NDA_ADMIN_SLACK_CHANNEL``,
    else an email fallback → ``NDA_ADMIN_EMAIL``). Imported lazily so this module carries no load-time
    dependency on the DB-touching approvals module (and so there is no import cycle: ``approvals`` imports
    ``GATED_INTENTS`` / ``GateDecision`` from here at module load, this imports ``approvals`` only at call
    time). Non-gated intents short-circuit inside the gate WITHOUT touching the DB.
    """
    from .approvals import AdminNotifier, AllowlistGate

    return AllowlistGate(
        settings=settings,
        notifier=AdminNotifier(service=service, post_blocks=post_blocks),
    )


# --------------------------------------------------------------------------- #
# Reply builders for the pipeline-owned turns (pending / error)
# --------------------------------------------------------------------------- #
def error_reply() -> IntentReply:
    """The ported friendly error reply (reference §2.7)."""
    from .intents import IntentReply

    return IntentReply(text=ERROR_REPLY_TEXT)


def pending_reply(intent: str, request_key: str) -> IntentReply:
    """The "awaiting approval" reply for a gated intent the user isn't (yet) allowed to run (PLAN §3.4).

    The ported UX (reference §3.5 "Reply: Pending Approval"), functional once the wave-B gate lands: the
    admin is notified out-of-band and the request auto-resumes on approval.
    """
    from .intents import IntentReply

    handle = f" (request `{request_key}`)" if request_key else ""
    return IntentReply(
        text=(
            f"Thanks — *{intent}* needs sign-off before I can run it. I've asked an admin to "
            f"approve you{handle}. I'll pick it up once you're approved."
        )
    )


def _admin_names_text(admin_names: tuple[str, ...]) -> str:
    return ", ".join(n for n in admin_names if n) if admin_names else "an admin"


def confirmation_reply(
    intent: str, request_key: str, admin_names: tuple[str, ...]
) -> IntentReply:
    """The Slack confirm-before-request card (PLAN §3.4): naming the approving admins + a *Request
    approval* button. Only on the button click does the admin get pinged (``request_approval`` kind)."""
    from .approvals import ACTION_REQUEST_APPROVAL, request_approval_button_value
    from .intents import IntentReply

    names = _admin_names_text(admin_names)
    text = f"*{intent}* needs sign-off from *{names}*. Want me to request approval?"
    blocks = (
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_REQUEST_APPROVAL,
                    "style": "primary",
                    "text": {
                        "type": "plain_text",
                        "text": "Request approval",
                        "emoji": True,
                    },
                    "value": request_approval_button_value(request_key),
                }
            ],
        },
    )
    return IntentReply(text=text, slack_blocks=blocks, fallback_text=text)


def requesting_reply(intent: str, admin_names: tuple[str, ...]) -> IntentReply:
    """The email (button-less) auto-advance reply: the admin has just been asked to approve."""
    from .intents import IntentReply

    return IntentReply(
        text=(
            f"Thanks — *{intent}* needs sign-off. I'm requesting approval from "
            f"{_admin_names_text(admin_names)} now, and I'll run it once you're approved."
        )
    )


# --------------------------------------------------------------------------- #
# Routing: deterministic -> classifier
# --------------------------------------------------------------------------- #
def route(
    envelope: Envelope,
    *,
    gateway: Gateway | None = None,
    settings: Settings | None = None,
) -> Classification:
    """The full routing decision for ``envelope``: deterministic first, LLM classifier on a defer.

    ``gateway`` overrides the classifier gateway (tests inject a fake — zero network). When routing
    defers and no gateway is available (none injected AND no LLM configured), the message degrades to
    ``unknown`` (→ help) instead of erroring. A live classifier PROVIDER failure propagates (the caller
    turns it into the friendly error reply, mirroring the n8n ``onError -> Flow Error Reply`` edge).
    """
    det = deterministic_route(envelope.text)
    if det is not None:
        return det

    from .classifier import build_classifier_gateway, classify

    gw = gateway
    if gw is None:
        from app.config import get_settings

        gw = build_classifier_gateway(settings or get_settings())
    if gw is None:
        log.warning(
            "bot.route.no_classifier_provider",
            event_key=envelope.event_key,
            note="deferred message but no LLM configured — degrading to unknown (help)",
        )
        return Classification(intent="unknown")
    return classify(envelope.text, gateway=gw)


# --------------------------------------------------------------------------- #
# Delivery seam (structural — the router never hard-imports the churning channels package)
# --------------------------------------------------------------------------- #
class _ReplyDeliverer(Protocol):
    """The subset of ``ReplyService`` the pipeline uses: deliver a text/file reply, fail-soft."""

    def deliver(self, envelope: Envelope, reply: Any) -> Any: ...


#: A Slack-only Block Kit poster (``SlackReplySink.post_blocks``): ``(envelope, blocks, fallback) -> ...``.
PostBlocks = Callable[[Envelope, list[dict], str], Any]

#: Process-wide delivery config the app assembly injects via :func:`configure_delivery`. Left ``None``
#: until wired — until then ``route_envelope`` computes + returns the reply but delivers nothing (a
#: loud, greppable no-op), so the pipeline is fully testable/usable before Slack/SMTP clients exist.
_DELIVERY: tuple[_ReplyDeliverer, PostBlocks | None] | None = None


def configure_delivery(
    service: _ReplyDeliverer, post_blocks: PostBlocks | None = None
) -> None:
    """Wire the channel-aware reply service the pipeline delivers through (called by app assembly).

    ``service`` is the ``ReplyService`` (Slack + email sinks registered); ``post_blocks`` is the Slack
    sink's ``post_blocks`` for interactive Block Kit cards (help / wave-B pickers). Idempotent — the
    last call wins.
    """
    global _DELIVERY
    _DELIVERY = (service, post_blocks)


def reset_delivery() -> None:
    """Clear the injected delivery config (tests / teardown)."""
    global _DELIVERY
    _DELIVERY = None


def _deliver(
    envelope: Envelope,
    reply: IntentReply,
    service: _ReplyDeliverer | None,
    post_blocks: PostBlocks | None,
) -> Any | None:
    """Deliver ``reply`` on the envelope's channel via the reply service (a no-op when none is wired).

    Slack + a blocks payload + a ``post_blocks`` capability → post the interactive Block Kit card; every
    other case delivers through the channel-aware service — the mrkdwn ``text`` plus any file
    ``attachments`` the handler produced (a wave-B ``template`` reply carries its .docx here, so the sink
    takes its file path: Slack ``files_upload_v2`` / an email attachment — the ported ``NDA: Reply File``
    branch). Email renders HTML/clean-text; a Slack text reply posts verbatim (reference §2.3 exact-
    ``slack`` fork). Fail-soft: any delivery error is logged and swallowed so the turn never crashes."""
    if service is None:
        log.info(
            "bot.deliver.skipped_no_service",
            event_key=envelope.event_key,
            note="no reply service configured (configure_delivery not called) — reply not sent",
        )
        return None
    try:
        if (
            reply.slack_blocks
            and envelope.channel == "slack"
            and post_blocks is not None
        ):
            return post_blocks(
                envelope, list(reply.slack_blocks), reply.fallback_text or reply.text
            )
        # Build the channel-agnostic text Reply lazily so this module carries no load-time dependency
        # on the channels package (owned by the Slack/email agents, actively evolving). ``attachments``
        # (empty for a text/blocks reply) forwards a handler-built file to the sink's file path.
        from .channels.protocol import Reply

        return service.deliver(
            envelope, Reply(text=reply.text, attachments=reply.attachments)
        )
    except Exception as exc:  # noqa: BLE001 — delivery is fail-soft; a reply failure never crashes
        log.warning("bot.deliver.failed", event_key=envelope.event_key, error=repr(exc))
        return None


# --------------------------------------------------------------------------- #
# The dispatch result + the pipeline
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DispatchResult:
    """The outcome of one turn — what the pipeline decided, said, and (optionally) delivered."""

    intent: str
    outcome: Literal["handled", "pending", "error"]
    reply: IntentReply
    deterministic: bool = False
    #: The reply-delivery result when a reply service was wired/supplied (else ``None``).
    delivery: Any | None = field(default=None)


def process(
    envelope: Envelope,
    *,
    registry: IntentRegistry | None = None,
    gate: ApprovalGate | None = None,
    gateway: Gateway | None = None,
    settings: Settings | None = None,
    service: _ReplyDeliverer | None = None,
    post_blocks: PostBlocks | None = None,
) -> DispatchResult:
    """Route → gate → dispatch → reply for one normalized envelope (PLAN §3.3/§3.4), fully injectable.

    The pure, testable core of the pipeline. ``registry`` defaults to
    :func:`app.bot.intents.default_registry` (``help`` only this wave); ``gate`` defaults to the real
    fail-CLOSED :class:`app.bot.approvals.AllowlistGate` (PLAN §3.4), wired to notify the admin through
    the supplied ``service`` / ``post_blocks``; ``gateway`` overrides the classifier LLM (fake in tests).
    When a reply ``service`` is supplied the reply is also delivered through it (Block Kit via
    ``post_blocks`` on Slack). Never raises — a classifier/handler failure yields the ported friendly
    error reply. Every step logs under ``event_key`` (bound as the correlation id so nested
    gateway/handler logs share it).
    """
    from .intents import IntentContext, default_registry

    registry = registry or default_registry()
    gate = gate or _default_gate(
        settings=settings, service=service, post_blocks=post_blocks
    )
    ek = envelope.event_key

    token = bind_correlation_id(ek)
    log.info(
        "bot.dispatch.start",
        event_key=ek,
        channel=envelope.channel,
        verified_sender=envelope.verified_sender,
        attachments=len(envelope.attachments),
    )
    try:
        try:
            classification = route(envelope, gateway=gateway, settings=settings)
            log.info(
                "bot.dispatch.routed",
                event_key=ek,
                intent=classification.intent,
                deterministic=classification.deterministic,
            )

            decision = gate.check(envelope, classification)
            if decision.status == "needs_confirmation":
                # The user isn't exempt; the request is stashed as awaiting_confirmation. Slack asks the
                # user to confirm (a *Request approval* button) BEFORE pinging admins; email can't show a
                # button, so it auto-advances (transition + admin ping) and confirms in words.
                intent = classification.intent
                if envelope.channel == "email":
                    advance = getattr(gate, "advance", None)
                    if callable(advance):
                        try:
                            advance(decision.request_key)
                        except Exception as exc:  # noqa: BLE001 — auto-advance is fail-soft
                            log.warning(
                                "bot.dispatch.advance_failed",
                                event_key=ek,
                                request_key=decision.request_key,
                                error=repr(exc),
                            )
                    reply = requesting_reply(intent, decision.admin_names)
                else:
                    reply = confirmation_reply(
                        intent, decision.request_key, decision.admin_names
                    )
                log.info(
                    "bot.dispatch.needs_confirmation",
                    event_key=ek,
                    intent=intent,
                    request_key=decision.request_key,
                    channel=envelope.channel,
                )
                delivery = _deliver(envelope, reply, service, post_blocks)
                return DispatchResult(
                    intent=intent,
                    outcome="pending",
                    reply=reply,
                    deterministic=classification.deterministic,
                    delivery=delivery,
                )
            if decision.status == "pending":
                log.info(
                    "bot.dispatch.pending",
                    event_key=ek,
                    intent=classification.intent,
                    request_key=decision.request_key,
                    reason=decision.reason,
                )
                reply = pending_reply(classification.intent, decision.request_key)
                delivery = _deliver(envelope, reply, service, post_blocks)
                return DispatchResult(
                    intent=classification.intent,
                    outcome="pending",
                    reply=reply,
                    deterministic=classification.deterministic,
                    delivery=delivery,
                )

            # Dispatch: unknown (and any unregistered intent) falls back to help (reference §1/§3.10).
            handler = registry.get(classification.intent)
            effective_intent = classification.intent
            if handler is None:
                handler = registry.get("help")
                effective_intent = "help"
                if (
                    handler is None
                ):  # a registry with no help fallback is a wiring bug — fail friendly
                    raise LookupError(
                        "no handler for intent and no 'help' fallback registered"
                    )
                log.info(
                    "bot.dispatch.fallback_help",
                    event_key=ek,
                    requested_intent=classification.intent,
                )

            reply = handler(
                IntentContext(envelope=envelope, classification=classification)
            )
            delivery = _deliver(envelope, reply, service, post_blocks)
            log.info(
                "bot.dispatch.handled",
                event_key=ek,
                intent=effective_intent,
                has_blocks=bool(reply.slack_blocks),
            )
            return DispatchResult(
                intent=effective_intent,
                outcome="handled",
                reply=reply,
                deterministic=classification.deterministic,
                delivery=delivery,
            )
        except Exception as exc:  # noqa: BLE001 — any routing/handler failure degrades to the friendly
            # error reply (reference §2.7), never crashes the turn — the n8n `onError -> Flow Error
            # Reply` edge on every risky node.
            log.error("bot.dispatch.error", event_key=ek, error=repr(exc))
            reply = error_reply()
            delivery = _deliver(envelope, reply, service, post_blocks)
            return DispatchResult(
                intent="error", outcome="error", reply=reply, delivery=delivery
            )
    finally:
        correlation_id_var.reset(token)


def route_envelope(
    envelope: Envelope,
    *,
    registry: IntentRegistry | None = None,
    gate: ApprovalGate | None = None,
    gateway: Gateway | None = None,
    settings: Settings | None = None,
    service: _ReplyDeliverer | None = None,
    post_blocks: PostBlocks | None = None,
) -> DispatchResult:
    """The production routing entry ``app.bot.dispatch.process_envelope`` calls after claiming an event.

    Thin wrapper over :func:`process`: it resolves the reply service (an explicit ``service`` argument
    wins; otherwise the process-wide config from :func:`configure_delivery`) so replies are delivered
    inside the pipeline (the worker contract: ``route_envelope``'s return value is ignored — it must do
    its own delivery). Extra keyword-only params keep it a drop-in for the ``Callable[[Envelope], object]``
    the dispatcher expects while staying override-friendly for tests. Never raises.
    """
    svc, pb = service, post_blocks
    if svc is None and _DELIVERY is not None:
        svc, pb = _DELIVERY
    return process(
        envelope,
        registry=registry,
        gate=gate,
        gateway=gateway,
        settings=settings,
        service=svc,
        post_blocks=pb,
    )
