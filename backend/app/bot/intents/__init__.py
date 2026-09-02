"""Intent handler registry + the help intent (PLAN §3.2/§3.4, reference §1 dispatch map, §3.10).

An intent handler is a pure, channel-agnostic function ``(IntentContext) -> IntentReply``: it inspects
the normalized :class:`~app.bot.envelope.Envelope` and the hardened
:class:`~app.bot.router.Classification`, and DESCRIBES the reply (mrkdwn text for email + Slack
fallback, plus optional Slack Block Kit for interactive cards). It never learns SMTP threading or the
Slack Web API — :mod:`app.bot.dispatch` delivers the described reply through the channel-aware reply
service (Block Kit via the Slack sink's ``post_blocks``, text via ``ReplyService.deliver``). This is the
"typed Pydantic-ish inputs/outputs" design PLAN §1 keeps so the handlers become MCP tool schemas later.

This wave lands ONLY the registry skeleton and the ``help`` intent (which doubles as the ``unknown``
fallback, reference §1/§3.10). ``template`` / ``generate`` / ``review`` / ``envelope`` / ``archive``
are wave B — they register onto this same :class:`IntentRegistry`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..envelope import Envelope
from ..router import Classification

if TYPE_CHECKING:
    from ..channels.protocol import OutboundAttachment

# --------------------------------------------------------------------------- #
# Handler contract
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IntentContext:
    """Everything an intent handler is given: the normalized message + the hardened routing decision.

    Frozen so a handler can't mutate what it was dispatched under. Wave-B handlers that need the DB,
    the LLM gateway, or correlation state will take them as explicit constructor deps on the handler
    object (kept off this context so the pure ``(ctx) -> reply`` shape stays trivially testable).
    """

    envelope: Envelope
    classification: Classification


@dataclass(frozen=True)
class IntentReply:
    """A channel-agnostic description of what to say back (delivered by the dispatcher).

    ``text`` is Slack-mrkdwn — the single authoring format (reference §2.5): the Slack sink posts it
    verbatim / as the blocks' fallback, the email sink renders it to HTML + clean text. ``slack_blocks``
    (when present) is a Slack Block Kit array the dispatcher posts via the Slack sink's ``post_blocks``
    on the Slack channel only; every other channel falls back to ``text``. ``fallback_text`` is Slack's
    required accessibility/notification text for a blocks post (defaults to ``text`` when blank).

    ``attachments`` (usually empty) carries handler-built files — the wave-B ``template`` intent returns
    the resolved ``.docx`` here, and the dispatcher's channel-aware delivery routes it to the sink's file
    path (Slack ``files_upload_v2`` / an email attachment — the ported ``NDA: Reply File`` branch), with
    ``text`` becoming the upload comment / email body. Mutually usable with ``slack_blocks`` only in the
    sense that a file reply carries no blocks (blocks win the Slack fork; every other reply delivers text
    + attachments).
    """

    text: str = ""
    slack_blocks: tuple[dict, ...] | None = None
    fallback_text: str = ""
    attachments: tuple[OutboundAttachment, ...] = field(default_factory=tuple)


#: A handler maps a context to a reply description. Sync — the gateway/DAL calls it wraps are sync.
IntentHandler = Callable[[IntentContext], IntentReply]


class IntentRegistry:
    """The intent → handler table the dispatcher routes through (reference §1 ``Route by Intent``)."""

    def __init__(self) -> None:
        self._handlers: dict[str, IntentHandler] = {}

    def register(self, intent: str, handler: IntentHandler) -> None:
        """Register (or replace) the handler for ``intent``. Last registration wins."""
        self._handlers[intent] = handler

    def get(self, intent: str) -> IntentHandler | None:
        return self._handlers.get(intent)

    def handles(self, intent: str) -> bool:
        return intent in self._handlers

    def intents(self) -> tuple[str, ...]:
        return tuple(self._handlers)


# --------------------------------------------------------------------------- #
# Help intent (reference §3.10) — also the `unknown` fallback
# --------------------------------------------------------------------------- #
#: Slack's required notification/fallback text for the help card (reference §3.10, verbatim).
HELP_FALLBACK_TEXT = (
    "NDA Assistant — mention me with: template, generate, review, envelope, or help."
)

#: One (title, mrkdwn body) per command — the single source for both the Slack card sections and the
#: plaintext email help, so the two never drift (reference §3.10 help copy).
_HELP_COMMANDS: tuple[tuple[str, str], ...] = (
    (
        "Template",
        "Get a blank NDA (.docx) to fill in yourself. Tell me the *jurisdiction* "
        "(US or SG) and *counterparty type* (company, service provider, or individual); "
        "for an individual, also the *mutuality* (mutual or unilateral).",
    ),
    (
        "Generate",
        "I fill in an NDA for you. I'll send a short form — complete it and I'll return "
        "the finished document.",
    ),
    (
        "Review",
        "Attach a `.docx` or `.pdf` and I'll run a quick automated review. "
        "_Restricted to approved users._",
    ),
    (
        "Envelope",
        "Send a clean NDA to DocuSign for signature. Needs *at least 2 signer emails*; "
        "supports sequential signing and CC timing.",
    ),
    ("Archive", "File a signed NDA into the Drive cache."),
    ("Help", "Show this message."),
)


def help_blocks() -> tuple[dict, ...]:
    """The ported Slack Block Kit help card (reference §3.10): header, a section per command, footer."""
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🔒 NDA Assistant", "emoji": True},
        }
    ]
    for title, body in _HELP_COMMANDS:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{title}* — {body}"},
            }
        )
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Mention me with: *template*, *generate*, *review*, "
                    "*envelope*, *archive*, or *help*.",
                }
            ],
        }
    )
    return tuple(blocks)


def help_text() -> str:
    """The plaintext help body for the email path (and the Slack fallback) — same copy as the card."""
    lines = ["🔒 *NDA Assistant* — here's what I can do:", ""]
    for title, body in _HELP_COMMANDS:
        lines.append(f"• *{title}* — {body}")
    lines.append("")
    lines.append(
        "Mention me with: template, generate, review, envelope, archive, or help."
    )
    return "\n".join(lines)


def help_intent(_ctx: IntentContext) -> IntentReply:
    """Return the help card (Slack) / help text (email). Channel-agnostic — the dispatcher picks the
    surface. Serves both the ``help`` intent and the ``unknown`` fallback (reference §1/§3.10)."""
    return IntentReply(
        text=help_text(),
        slack_blocks=help_blocks(),
        fallback_text=HELP_FALLBACK_TEXT,
    )


def default_registry() -> IntentRegistry:
    """The registry the dispatcher uses by default (reference §1 dispatch map).

    ``help`` (also the ``unknown`` fallback) plus the wave-B ``template`` / ``review`` handlers. The two
    handler classes are imported lazily and constructed with their production defaults (DB session
    factory, engine path, Slack file fetcher resolved from settings at call time) so importing this
    module — which the dispatcher does on every turn — stays cheap and free of the heavy engine imports.
    ``envelope`` / ``archive`` land in later phases and register onto this same registry.
    """
    from .archive import ArchiveIntent
    from .envelope import EnvelopeIntent
    from .expiration import ExpirationIntent
    from .generate import GenerateIntent
    from .review import ReviewIntent
    from .template import TemplateIntent
    from .template_admin import with_template_admin

    registry = IntentRegistry()
    registry.register("help", help_intent)
    # The template intent is wrapped so an ADMIN sender's picker card gains an "Update this template"
    # button (PLAN §3.7 Slack guided template-replacement flow). A non-admin's reply is untouched — no
    # button — so only admins can enter the self-serve update chain (app.bot.intents.template_admin).
    registry.register("template", with_template_admin(TemplateIntent()))
    registry.register("review", ReviewIntent())
    registry.register("generate", GenerateIntent())
    registry.register("envelope", EnvelopeIntent())
    registry.register("archive", ArchiveIntent())
    # P4 manual expiration commands (PLAN §3.10 trigger c). The handler is registered here so it is
    # dispatchable; the deterministic ROUTER that makes ``set/re-extract expiration …`` route to it is
    # frozen this wave — the one-line router change (a branch calling
    # ``app.bot.intents.expiration.matches_expiration_command`` + adding ``"expiration"`` to the
    # router's ``INTENTS`` set) is noted for the integrator in the task open_items.
    registry.register("expiration", ExpirationIntent())
    return registry


__all__ = [
    "IntentContext",
    "IntentReply",
    "IntentHandler",
    "IntentRegistry",
    "default_registry",
    "help_intent",
    "help_blocks",
    "help_text",
    "HELP_FALLBACK_TEXT",
]
