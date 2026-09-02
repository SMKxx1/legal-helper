"""The ``generate`` intent — hand the user the NDA **Tally** form link (PLAN §3.6, reference §3.3).

A behavioral port of the n8n ``NDA: Generate`` sub-workflow (ground-truth reference §3.3): the reply is
a link to the public Tally "NDA Generator" form, with the requester's conversation **pre-filled** into
the form's hidden ``channel`` / ``thread_ts`` fields so the Tally webhook
(:mod:`app.api.routes_tally`) can deliver the finished NDA back to the RIGHT thread / email. This
replaces the retired in-house ``/f`` form service: the routing state that used to be minted into a
signed ``/f`` link now rides the Tally URL query string exactly as the original Tally flow did
(``channel`` = ``email||<addr>`` for email or the Slack channel id for Slack; ``thread_ts`` for the
Slack thread).

The reply mirrors the ported two-branch shape (reference §3.3): on Slack a Block Kit section + a URL
button carrying the preserved label **"Open the NDA form"**; on email the ported threaded reply with
the link inline. The intent does NOT generate a document — that happens on submit, via the Tally
webhook → :func:`app.bot.flows.generate_completion.run_generation`.

Capability-gated (PLAN §3.4): when the ``tally`` capability is disabled (``tally_signing_secret``
missing, so a submission could not be verified/processed) the handler degrades to a friendly "not
available" reply instead of handing out a dead-end link — capabilities fail soft (PLAN §6).

Like the other wave-B intents this is a channel-agnostic ``(IntentContext) -> IntentReply`` with no
side effects (the link is built from config + the envelope). Collaborators (settings, the capability
registry) are injected constructor deps resolved lazily, so the reply matrix is unit-tested with a
``Settings(_env_file=None, …)`` and zero network (PLAN house rules).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from ...capabilities import TALLY, CapabilityState
from ...telemetry import get_logger
from . import IntentContext, IntentReply

if TYPE_CHECKING:
    from ...capabilities import CapabilityRegistry
    from ...config import Settings

log = get_logger("nda.bot.intent.generate")

# --------------------------------------------------------------------------- #
# Preserved copy (reference §3.3) + contract identifiers
# --------------------------------------------------------------------------- #
#: The URL-button label — a PRESERVED contract string (reference §3.3 ``Slack: Generate Link``).
OPEN_FORM_BUTTON_TEXT = "Open the NDA form"

#: The Slack card's section body (reference §3.3 section text) — reused as the email lead line.
GENERATE_SECTION_TEXT = "Fill in this form and I'll send back the completed NDA."

#: Slack's required notification/accessibility fallback for the card (reference §3.3 fallback text).
GENERATE_FALLBACK_TEXT = "Fill in the NDA form to generate your document."

#: The URL button's ``action_id``. The button is a LINK button (Slack opens ``url`` in the browser),
#: but Slack still POSTs a block_actions interaction on click — the ported flow IGNORES it. The
#: interactivity registry maps this id to ``KIND_IGNORE`` (else the dispatcher would post the "button
#: expired" reply on every form-open). Kept verbatim.
ACTION_OPEN_FORM = "open_nda_form"

#: The friendly degrade when the ``tally`` capability is off (capabilities fail soft, PLAN §6).
FORMS_UNAVAILABLE_TEXT = (
    "Sorry — the NDA form isn't available right now, so I can't start a generation. "
    "Please try again later, or ask the team to check the form service configuration."
)


class GenerateIntent:
    """The ``generate`` intent handler (reference §3.3). Callable ``(ctx) -> IntentReply``.

    ``settings`` supplies the Tally link config (``tally_base_url`` / ``tally_form_id``); ``registry`` is
    the capability registry the ``tally`` gate reads (defaults to one built from settings). Both lazy so
    importing this module carries no engine load; tests inject a ``Settings(_env_file=None, …)`` and a
    registry and drive the whole reply matrix with no network.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        registry: CapabilityRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry

    # -- lazy production defaults ------------------------------------------
    def _get_settings(self) -> Settings:
        if self._settings is not None:
            return self._settings
        from ...config import get_settings

        return get_settings()

    def _get_registry(self, settings: Settings) -> CapabilityRegistry:
        if self._registry is not None:
            return self._registry
        from ...capabilities import build_registry

        return build_registry(settings)

    # -- entry point -------------------------------------------------------
    def __call__(self, ctx: IntentContext) -> IntentReply:
        envelope = ctx.envelope
        settings = self._get_settings()

        # Capability gate FIRST (PLAN §3.4): a disabled ``tally`` capability degrades to a friendly
        # reply, never a dead-end link whose submission could not be processed.
        if self._get_registry(settings).state(TALLY) is not CapabilityState.ENABLED:
            log.info(
                "bot.intent.generate.tally_disabled",
                event_key=envelope.event_key,
                channel=envelope.channel,
            )
            return IntentReply(text=FORMS_UNAVAILABLE_TEXT)

        url = _tally_form_url(envelope, settings)
        log.info(
            "bot.intent.generate.link_handed",
            event_key=envelope.event_key,
            channel=envelope.channel,
        )
        if envelope.channel == "slack":
            return IntentReply(
                slack_blocks=tuple(_generate_blocks(url)),
                fallback_text=GENERATE_FALLBACK_TEXT,
            )
        return IntentReply(text=_email_text(url))


def _tally_form_url(envelope: Any, settings: Settings) -> str:
    """Build the public Tally form URL with the requester's routing pre-filled into the hidden fields.

    The routing (``channel`` = ``email||<addr>`` or the Slack channel id, plus the Slack ``thread_ts``)
    is SIGNED into a single ``channel`` token via :func:`app.integrations.tally.mint_routing_token`, so a
    respondent can't edit the URL to redirect the generated NDA — the webhook only trusts a valid token.
    A bare submission (no token) still generates the doc; it just won't auto-route a reply back."""
    from app.integrations.tally import mint_routing_token

    base = (settings.tally_base_url or "https://tally.so").rstrip("/")
    form_id = settings.tally_form_id or "jagDPJ"
    if envelope.channel == "slack":
        channel_param = envelope.slack_channel or ""
        thread_ts = envelope.slack_thread_ts or ""
    else:
        addr = envelope.sender_address or ""
        channel_param = f"email||{addr}" if addr else ""
        thread_ts = ""
    url = f"{base}/r/{form_id}"
    if channel_param:
        token = mint_routing_token(
            settings.tally_signing_secret, channel_param, thread_ts
        )
        url = f"{url}?{urlencode({'channel': token})}"
    return url


def _generate_blocks(url: str) -> list[dict]:
    """The ported ``Slack: Generate Link`` card (reference §3.3): a section + a URL button.

    The button label is the preserved ``Open the NDA form`` contract string.
    """
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": GENERATE_SECTION_TEXT},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_OPEN_FORM,
                    "style": "primary",
                    "text": {
                        "type": "plain_text",
                        "text": OPEN_FORM_BUTTON_TEXT,
                        "emoji": True,
                    },
                    "url": url,
                }
            ],
        },
    ]


def _email_text(url: str) -> str:
    """The ported threaded email reply (reference §3.3 email branch): the lead line + the link inline."""
    return f"{GENERATE_SECTION_TEXT}\n\n{url}"


__all__ = [
    "GenerateIntent",
    "OPEN_FORM_BUTTON_TEXT",
    "GENERATE_SECTION_TEXT",
    "GENERATE_FALLBACK_TEXT",
    "ACTION_OPEN_FORM",
    "FORMS_UNAVAILABLE_TEXT",
]
