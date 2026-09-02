"""Typed Block Kit builders (PLAN §3.3, §3.7) — the help card and the template picker.

These two interactive cards are the Slack surface the router's help/template intents post. Their
``action_id``s and select option VALUES are a PRESERVED CONTRACT: ``NDA: Interactivity`` (now the
router's interactivity handler) routes on them verbatim (reference §3.2, §3.7, §8), so a drift here
silently breaks the button/modal callbacks. The identifiers and option values below are therefore
module constants, re-exported for the interactivity handler to match against — never re-typed inline.

The builders return plain Block Kit JSON (``list[dict]``) — that is exactly the payload the Slack Web
API expects, and keeping them as data (not slack_sdk builder objects) makes them trivially assertable
in tests with no Slack dependency. ``*_FALLBACK_TEXT`` is the notification/accessibility text posted
alongside ``blocks`` (Slack requires it wherever blocks can't render).
"""

from __future__ import annotations

import json
from typing import Any

#: A single Block Kit block (a JSON object). Aliased for readable builder signatures.
Block = dict[str, Any]

# --------------------------------------------------------------------------------------------------
# Preserved contract — action_ids + select option values (reference §3.2 "Slack: Template Picker", §8)
# --------------------------------------------------------------------------------------------------
ACTION_SELECT_JURISDICTION = "select_jurisdiction"
ACTION_SELECT_COUNTERPARTY_TYPE = "select_counterparty_type"
ACTION_SELECT_MUTUALITY = "select_mutuality"
ACTION_TEMPLATE_SUBMIT = "template_submit"

#: Option VALUES are the exact codes the picker returns and the template DAL normalizes (reference
#: §2.10, §6 "picker option allowlists"). The display labels are cosmetic; the values are the contract.
JURISDICTION_OPTIONS: tuple[str, ...] = ("US", "SG")
COUNTERPARTY_TYPE_OPTIONS: tuple[str, ...] = (
    "service_provider",
    "company",
    "individual",
)
MUTUALITY_OPTIONS: tuple[str, ...] = ("mutual", "unilateral")

_JURISDICTION_LABELS = {"US": "United States (US)", "SG": "Singapore (SG)"}
_COUNTERPARTY_TYPE_LABELS = {
    "service_provider": "Service Provider",
    "company": "Company",
    "individual": "Individual",
}
_MUTUALITY_LABELS = {"mutual": "Mutual", "unilateral": "Unilateral"}

#: Notification/accessibility fallbacks (reference §3.10 help fallback; picker guidance text).
HELP_FALLBACK_TEXT = (
    "NDA Assistant — mention me with: template, generate, review, envelope, or help."
)
TEMPLATE_PICKER_FALLBACK_TEXT = (
    "Choose a jurisdiction and counterparty type (plus mutuality for an individual), "
    "then press Get template."
)

#: The command reference shown on the help card (reference §3.10 help copy). One section per command.
_HELP_COMMANDS: tuple[tuple[str, str], ...] = (
    (
        "📄 Template",
        "Get an empty NDA template. Pick a *jurisdiction* (US / SG) and *counterparty type* "
        "(service provider, company, individual — individuals also pick *mutuality*).",
    ),
    (
        "📝 Generate",
        "Fill in a short form and I'll send back the completed NDA document.",
    ),
    (
        "🔍 Review",
        "Attach a `.docx` or `.pdf` and I'll run a quick automated review. "
        "_Restricted to approved users._",
    ),
    (
        "📨 Envelope",
        "Send a clean NDA to DocuSign for signature (needs *≥2 signer emails*; supports "
        "sequential signing and CC timing). _Restricted to approved users._",
    ),
    (
        "📥 Archive",
        "File a signed NDA into the Signed NDAs storage.",
    ),
    (
        "❓ Help",
        "Show this message.",
    ),
)


def _text(text: str, *, kind: str = "mrkdwn") -> dict[str, Any]:
    """A Block Kit text composition object (``mrkdwn`` by default, ``plain_text`` for select labels)."""
    obj: dict[str, Any] = {"type": kind, "text": text}
    if kind == "plain_text":
        obj["emoji"] = True
    return obj


def _option(value: str, label: str) -> dict[str, Any]:
    return {"text": _text(label, kind="plain_text"), "value": value}


def _static_select(
    action_id: str, placeholder: str, options: tuple[str, ...], labels: dict[str, str]
) -> Block:
    """One ``static_select`` accessory block-with-label (the picker's rows)."""
    return {
        "type": "section",
        "text": _text(f"*{placeholder}*"),
        "accessory": {
            "type": "static_select",
            "action_id": action_id,
            "placeholder": _text(placeholder, kind="plain_text"),
            "options": [_option(v, labels.get(v, v)) for v in options],
        },
    }


def help_blocks() -> list[Block]:
    """The ``🔒 NDA Assistant`` help card (reference §3.10 ``Slack: Help Block``)."""
    blocks: list[Block] = [
        {
            "type": "header",
            "text": _text("🔒 NDA Assistant", kind="plain_text"),
        },
    ]
    for title, body in _HELP_COMMANDS:
        blocks.append({"type": "section", "text": _text(f"*{title}*\n{body}")})
    blocks.append(
        {
            "type": "context",
            "elements": [
                _text(
                    "Mention me with: template · generate · review · envelope · archive · help"
                )
            ],
        }
    )
    return blocks


def template_picker_blocks() -> list[Block]:
    """The ``📄 Template selection`` picker (reference §3.2 ``Slack: Template Picker``).

    Three ``static_select``s (jurisdiction / counterparty type / mutuality) + a primary *Get template*
    button (``template_submit``) + the ported context note. The button click is handled by the router's
    interactivity handler, which reads the selected values by their preserved ``action_id``s.
    """
    return [
        {
            "type": "header",
            "text": _text("📄 Template selection", kind="plain_text"),
        },
        _static_select(
            ACTION_SELECT_JURISDICTION,
            "Jurisdiction",
            JURISDICTION_OPTIONS,
            _JURISDICTION_LABELS,
        ),
        _static_select(
            ACTION_SELECT_COUNTERPARTY_TYPE,
            "Counterparty type",
            COUNTERPARTY_TYPE_OPTIONS,
            _COUNTERPARTY_TYPE_LABELS,
        ),
        _static_select(
            ACTION_SELECT_MUTUALITY,
            "Mutuality",
            MUTUALITY_OPTIONS,
            _MUTUALITY_LABELS,
        ),
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_TEMPLATE_SUBMIT,
                    "style": "primary",
                    "text": _text("Get template", kind="plain_text"),
                }
            ],
        },
        {
            "type": "context",
            "elements": [
                _text(
                    "Mutuality is ignored for Service Provider and Company templates."
                )
            ],
        },
    ]


# ==================================================================================================
# Envelope / DocuSign cards + modal (PLAN §3.9, reference §3.5/§3.7)
# --------------------------------------------------------------------------------------------------
# The interactive surface of the envelope flow. The ``action_id`` / ``callback_id`` / modal block-id
# strings below are the PRESERVED interactivity contract (reference §3.7, §8): the envelope kind
# handlers (``app.bot.intents.envelope``) match on them verbatim, so a drift here silently breaks the
# button/modal callbacks. Button VALUES carry only a versioned ``{v, kind, ref}`` — the correlation
# ``ref`` keys a ``bot_correlation`` row that holds the real state (doc bytes + routing + requester),
# so the value never approaches Slack's 2000-char limit (PLAN §3.9 "typed payloads carry only keys").
# ==================================================================================================

#: The typed button-value version (mirrors ``app.bot.interactivity.PAYLOAD_VERSION`` — kept here so
#: blockkit stays free of an interactivity import; the two MUST agree).
PAYLOAD_VERSION = 1

# -- kinds (the interactivity discriminator carried in each value / resolved from each id) ---------
KIND_SEND_DOCUSIGN = "send_docusign"
KIND_ENV_CONFIRM = "env_confirm"
KIND_ENV_USE_DOC = "env_use_doc"
KIND_DECLINE_DOC = "decline_doc"
#: The archive no-file thread-doc recovery confirm (PLAN §3.10, reference §3.7 ``arch_use_doc``). Its
#: "No, attach a file" button reuses the shared ``decline_doc`` kind (identical reply), so only the
#: "Yes, archive it" kind is new here.
KIND_ARCH_USE_DOC = "arch_use_doc"
#: The modal submit's kind (resolved from its ``callback_id``, not a button value).
KIND_NDA_DOCUSIGN_MODAL = "nda_docusign"

# -- block_actions action_ids (reference §3.7, §8) -------------------------------------------------
#: "Enter signing details" (opens the modal) — the ported ``send_docusign`` contract (reference §3.5).
ACTION_SEND_DOCUSIGN = "send_docusign"
#: The NEW confirm card's two buttons (PLAN §2 change #1 — human confirm before DocuSign send).
ACTION_ENV_CONFIRM_SEND = "env_confirm_send"
ACTION_ENV_CONFIRM_CANCEL = "env_confirm_cancel"
#: The thread-doc recovery confirm chain (reference §3.5 ``env_use_doc`` / ``decline_doc``).
ACTION_ENV_USE_DOC = "env_use_doc"
ACTION_DECLINE_DOC = "decline_doc"
#: The archive thread-doc recovery confirm (reference §3.6/§3.7 ``arch_use_doc``). Its decline reuses
#: ``ACTION_DECLINE_DOC`` (the same "attach a file" reply), so no separate archive-decline action.
ACTION_ARCH_USE_DOC = "arch_use_doc"

# -- modal (views.open) — callback + block/action ids (reference §3.7 "Slack config") --------------
MODAL_CALLBACK_ID = "nda_docusign"
MODAL_BLOCK_AMP = "b_amp"
MODAL_ACTION_AMP = "amp_email"
MODAL_BLOCK_CP = "b_cp"
MODAL_ACTION_CP = "cp_email"
MODAL_BLOCK_SEQ = "b_seq"
MODAL_ACTION_SEQ = "seq"
MODAL_BLOCK_CC = "b_cc"
MODAL_ACTION_CC = "cc"
MODAL_BLOCK_CC_SEQ = "b_cc_seq"
MODAL_ACTION_CC_SEQ = "cc_seq"

#: Signing-order option VALUES (reference §3.7). The contract the modal returns and the DocuSign
#: request builder consumes (``app.integrations.docusign`` ``ROUTING_*``).
SIGNING_ORDER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("all_at_once", "Everyone at once"),
    ("amp_first", "Amperesand signs first"),
    ("cp_first", "Counterparty signs first"),
)
#: CC-timing option VALUES (reference §3.7): before => routingOrder 1; after => after the last signer.
CC_TIMING_OPTIONS: tuple[tuple[str, str], ...] = (
    ("after", "After the signers"),
    ("before", "Before the signers"),
)

#: Notification/accessibility fallbacks for the envelope cards (Slack shows these where blocks can't).
ENVELOPE_CONFIRM_FALLBACK_TEXT = (
    "Review the signature request, then press Confirm & send (or Cancel)."
)
SEND_DOCUSIGN_FALLBACK_TEXT = (
    "Press Enter signing details to send this NDA for signature."
)
THREAD_DOC_FALLBACK_TEXT = (
    "I found a document in this thread — use it, or attach a file."
)
ARCH_THREAD_DOC_FALLBACK_TEXT = (
    "I found a document in this thread — archive it, or attach a file."
)


def _button(
    action_id: str, label: str, value: str, *, style: str | None = None
) -> Block:
    """One Block Kit button element with a JSON string ``value`` (the typed, versioned payload)."""
    btn: dict[str, Any] = {
        "type": "button",
        "action_id": action_id,
        "text": _text(label, kind="plain_text"),
        "value": value,
    }
    if style:
        btn["style"] = style
    return btn


# -- typed button values (single source; the kind handlers validate against the same schema) -------
def _value(kind: str, ref: str, **extra: Any) -> str:
    payload: dict[str, Any] = {"v": PAYLOAD_VERSION, "kind": kind, "ref": ref}
    payload.update(extra)
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def send_docusign_value(ref: str) -> str:
    """The ``send_docusign`` button value: ``{v:1, kind:"send_docusign", ref}`` (opens the modal).

    This is the SAME value the generate flow emits after delivering a generated .docx (PLAN §3.9,
    task deliverable 3) — ``ref`` keys the ``bot_correlation`` row holding that document.
    """
    return _value(KIND_SEND_DOCUSIGN, ref)


def env_confirm_value(ref: str, action: str) -> str:
    """A confirm-card button value: ``{v:1, kind:"env_confirm", action:"send"|"cancel", ref}``."""
    return _value(KIND_ENV_CONFIRM, ref, action=action)


def env_use_doc_value(ref: str) -> str:
    """The "use this thread doc" button value: ``{v:1, kind:"env_use_doc", ref}``."""
    return _value(KIND_ENV_USE_DOC, ref)


def arch_use_doc_value(ref: str) -> str:
    """The "archive this thread doc" button value: ``{v:1, kind:"arch_use_doc", ref}`` (reference §3.6/§3.7).

    ``ref`` keys the ``bot_correlation`` row holding the recovered thread document + its channel/thread
    origin, so the ``arch_use_doc`` handler (``app.bot.intents.archive``) files the right file."""
    return _value(KIND_ARCH_USE_DOC, ref)


def decline_doc_value(ref: str) -> str:
    """The "no, attach a file" button value: ``{v:1, kind:"decline_doc", ref}``."""
    return _value(KIND_DECLINE_DOC, ref)


def envelope_confirm_blocks(summary_mrkdwn: str, ref: str) -> list[Block]:
    """The NEW explicit confirm card (PLAN §2 change #1, §3.9a): a signer/routing/CC summary + the
    *Confirm & send* / *Cancel* buttons. The actual DocuSign send happens ONLY on the Confirm click —
    closing the spoofed-email → outbound-envelope hole (PLAN §6). ``ref`` keys the stored envelope
    state; both button values carry it plus their authoritative ``action``.
    """
    return [
        {
            "type": "header",
            "text": _text("📨 Ready to send for signature", kind="plain_text"),
        },
        {"type": "section", "text": _text(summary_mrkdwn)},
        {
            "type": "actions",
            "elements": [
                _button(
                    ACTION_ENV_CONFIRM_SEND,
                    "Confirm & send",
                    env_confirm_value(ref, "send"),
                    style="primary",
                ),
                _button(
                    ACTION_ENV_CONFIRM_CANCEL,
                    "Cancel",
                    env_confirm_value(ref, "cancel"),
                    style="danger",
                ),
            ],
        },
    ]


def signer_details_button_blocks(file_name: str, ref: str) -> list[Block]:
    """The <2-signer entry (PLAN §3.9b): confirm the document + an *Enter signing details* button that
    opens the modal (reference §3.5 ``Form Resend Doc`` → ``send_docusign``). ``ref`` keys the stored
    document; the modal carries it in ``private_metadata``."""
    return [
        {
            "type": "section",
            "text": _text(
                f"I'll send *{file_name}* for signature. Add the signing details and "
                "I'll show you a final confirmation before anything goes out."
            ),
        },
        {
            "type": "actions",
            "elements": [
                _button(
                    ACTION_SEND_DOCUSIGN,
                    "Enter signing details",
                    send_docusign_value(ref),
                    style="primary",
                )
            ],
        },
    ]


def thread_doc_confirm_blocks(file_name: str, ref: str) -> list[Block]:
    """The no-file thread-doc recovery confirm (PLAN §3.9c, reference §3.5 ``Build Env Confirm``):
    *Yes, use it* (``env_use_doc``) / *No, attach a file* (``decline_doc``). Both values carry ``ref``."""
    return [
        {
            "type": "section",
            "text": _text(
                f"I found *{file_name}* earlier in this thread. Want me to use it for "
                "the signature request?"
            ),
        },
        {
            "type": "actions",
            "elements": [
                _button(
                    ACTION_ENV_USE_DOC,
                    "Yes, use it",
                    env_use_doc_value(ref),
                    style="primary",
                ),
                _button(
                    ACTION_DECLINE_DOC,
                    "No, attach a file",
                    decline_doc_value(ref),
                ),
            ],
        },
    ]


def arch_confirm_blocks(file_name: str, ref: str) -> list[Block]:
    """The archive no-file thread-doc recovery confirm (PLAN §3.10, reference §3.6/§3.7 ``Build Arch Confirm``):
    *Yes, archive it* (``arch_use_doc``) / *No, attach a file* (``decline_doc``). Both values carry ``ref``
    (the decline reuses the shared ``decline_doc`` kind — same "attach a file" reply as the envelope path)."""
    return [
        {
            "type": "section",
            "text": _text(
                f"I found *{file_name}* earlier in this thread. Want me to archive it to the "
                "Signed NDAs storage?"
            ),
        },
        {
            "type": "actions",
            "elements": [
                _button(
                    ACTION_ARCH_USE_DOC,
                    "Yes, archive it",
                    arch_use_doc_value(ref),
                    style="primary",
                ),
                _button(
                    ACTION_DECLINE_DOC,
                    "No, attach a file",
                    decline_doc_value(ref),
                ),
            ],
        },
    ]


def _modal_email_input(block_id: str, action_id: str, label: str) -> Block:
    """A single-line email input row (``plain_text_input``, required)."""
    return {
        "type": "input",
        "block_id": block_id,
        "label": _text(label, kind="plain_text"),
        "element": {
            "type": "plain_text_input",
            "action_id": action_id,
            "placeholder": _text("name@company.com", kind="plain_text"),
        },
    }


def _modal_static_select(
    block_id: str,
    action_id: str,
    label: str,
    options: tuple[tuple[str, str], ...],
) -> Block:
    """A required ``static_select`` input row with the first option pre-selected (the ported default)."""
    opts = [_option(value, text) for value, text in options]
    return {
        "type": "input",
        "block_id": block_id,
        "label": _text(label, kind="plain_text"),
        "element": {
            "type": "static_select",
            "action_id": action_id,
            "options": opts,
            "initial_option": opts[0],
        },
    }


def docusign_modal_view(ref: str, *, file_name: str = "") -> dict[str, Any]:
    """The ``nda_docusign`` modal view (reference §3.7 ``Build Modal``) — a COLLECTOR, not a guard.

    Collects Amperesand + counterparty signer emails, signing order, CC emails and CC timing (the
    ported fields/labels/option values). ``ref`` rides in ``private_metadata`` so the view_submission
    resolves the stored document + requester context; on submit the handler builds the SAME confirm
    card (the send still waits for the explicit Confirm click).
    """
    title_suffix = f" — {file_name}" if file_name else ""
    return {
        "type": "modal",
        "callback_id": MODAL_CALLBACK_ID,
        "private_metadata": ref,
        "title": _text("Send for signature", kind="plain_text"),
        "submit": _text("Review & confirm", kind="plain_text"),
        "close": _text("Cancel", kind="plain_text"),
        "blocks": [
            {
                "type": "context",
                "elements": [_text(f"Signing details for this NDA{title_suffix}.")],
            },
            _modal_email_input(
                MODAL_BLOCK_AMP, MODAL_ACTION_AMP, "Amperesand signer email"
            ),
            _modal_email_input(
                MODAL_BLOCK_CP, MODAL_ACTION_CP, "Counterparty signer email"
            ),
            _modal_static_select(
                MODAL_BLOCK_SEQ,
                MODAL_ACTION_SEQ,
                "Signing order",
                SIGNING_ORDER_OPTIONS,
            ),
            {
                "type": "input",
                "block_id": MODAL_BLOCK_CC,
                "optional": True,
                "label": _text("CC emails (optional)", kind="plain_text"),
                "element": {
                    "type": "plain_text_input",
                    "action_id": MODAL_ACTION_CC,
                    "multiline": True,
                    "placeholder": _text(
                        "One per line (or comma-separated)", kind="plain_text"
                    ),
                },
            },
            _modal_static_select(
                MODAL_BLOCK_CC_SEQ, MODAL_ACTION_CC_SEQ, "CC timing", CC_TIMING_OPTIONS
            ),
        ],
    }
