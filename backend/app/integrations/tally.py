"""Tally intake — webhook signature verification + submission → engine-token mapping.

Replaces the in-house ``/f`` form service (retired) with the external **Tally** "NDA Generator" form
(``formId`` default ``jagDPJ``). Tally POSTs a ``FORM_RESPONSE`` webhook on submit; the route
(:mod:`app.api.routes_tally`) verifies the signature, maps the fields to the engine token table +
routing selectors, and hands off to the shared generation flow
(:func:`app.bot.flows.generate_completion.run_generation`).

AUTH — Tally signs each webhook with an HMAC-SHA256 over the raw request body using the form's signing
secret, base64-encoded, in the ``tally-signature`` header. :func:`verify_signature` recomputes and
constant-time compares; it fails CLOSED (empty secret or header → reject).

MAPPING — the label→token table + routing + service-provider detection below were validated against
all 27 real submissions of the live form (regression cases in ``tests/test_tally.py``). It is a single
branching form (US/SG × Company/Individual/ServiceProvider × Mutual/Unilateral); only one branch's
fields are answered per submission, so mapping by (normalized label, field type) is unambiguous.

REQUESTER ROUTING — the form carries hidden fields ``channel`` (``email||<addr>`` or a bare Slack
channel id like ``C0…``) and ``thread_ts``, pre-filled by the bot's generate intent when it hands out
the link. :func:`origin_context` turns them into the ``envelope_context`` shape
:func:`app.bot.flows.generate_completion._origin_envelope` consumes, so the finished NDA lands back in
the originating conversation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..telemetry import get_logger

log = get_logger("nda.integrations.tally")

#: Tally webhook signature header (HMAC-SHA256 of the raw body, base64).
SIGNATURE_HEADER = "tally-signature"

# --------------------------------------------------------------------------- #
# Field → engine-token map (validated against 27 live submissions)
# --------------------------------------------------------------------------- #
#: Normalized (lowercased, stripped) question label → engine token name. Multiple form branches use
#: different labels for the same token; every alias maps here.
LABEL2TOKEN: dict[str, str] = {
    "party b legal entity name": "counterparty_name",
    "receiving party name": "counterparty_name",
    "receiving party legal name": "counterparty_name",
    "individual full name": "counterparty_name",
    "counterparty full name": "counterparty_name",
    "jurisdiction of incorporation": "jurisdiction",
    "company registration number": "counterparty_company_registration_number",
    "company registration number / uen": "counterparty_company_registration_number",
    "last four digits only": "individual_id_number",
    "e.g., nric ending 1234 — last four digits only": "individual_id_number",
    "registered address": "street_address",
    "street address": "street_address",
    "city, state/province": "city_zip",
    "country, zip/postal code": "country",
    "amperesand signer full name": "amperesand_signer_name",
    "amperesand signer title": "amperesand_signer_title",
    "party b signer full name": "counterparty_signer_name",
    "receiving party signer full name": "counterparty_signer_name",
    "party b signer title": "counterparty_signer_title",
    "receiving party signer title": "counterparty_signer_title",
    "receiving signer title": "counterparty_signer_title",
    "receiving party title": "counterparty_signer_title",
    "describe the purpose of disclosure": "purpose",
    "relationship relating to": "services",
    "description of services": "services",
}

#: Routing dropdowns (normalized label → selector). ``entitity`` is the live form's typo — kept.
_ROUTING = {
    "us / sg": "jurisdiction",
    "counterparty entitity type": "counterparty_type",
    "counterparty entity type": "counterparty_type",
    "mutual / unilateral": "mutuality",
}

#: Field types whose value is an option-id list to resolve against the field's ``options``.
_OPTION_TYPES = {"DROPDOWN", "MULTIPLE_CHOICE", "CHECKBOXES", "MULTI_SELECT", "RANKING"}


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def label_to_token(label: str | None, field_type: str | None) -> str | None:
    """Map one answered field to an engine token by (normalized label, type). Returns ``None`` for
    fields with no token (routing dropdowns, the submit checkbox, hidden fields — filtered upstream)."""
    n = _norm(label)
    if field_type == "INPUT_EMAIL":
        return "notice_email"
    if field_type == "INPUT_DATE":
        return "effective_date"
    if n in LABEL2TOKEN:
        return LABEL2TOKEN[n]
    if (
        n.startswith("attn")
        or "general counsel" in n
        or n.startswith("e.g., jone doe")
        or n.startswith("e.g., jane doe")
    ):
        return "attn"
    if n.startswith("e.g., explore"):
        return "purpose"
    return None


# --------------------------------------------------------------------------- #
# Signature
# --------------------------------------------------------------------------- #
def verify_signature(secret: str, raw_body: bytes, header: str | None) -> bool:
    """True iff ``header`` is the valid Tally HMAC-SHA256 (base64) of ``raw_body`` under ``secret``.

    Fails CLOSED: an empty secret or missing header returns False (the route rejects with 401)."""
    if not secret or not header:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, header.strip())


# --------------------------------------------------------------------------- #
# Signed routing token — binds the reply destination to a bot-issued link
# --------------------------------------------------------------------------- #
# The retired in-house ``/f`` link was a SIGNED fragment token, so the reply destination could not be
# forged. Tally hidden fields are populated from URL query params the RESPONDENT controls, so a raw
# ``channel`` value must NOT be trusted: anyone could open ``…/r/<form>?channel=email||victim@x`` and
# have the company-authored NDA delivered from the company address to an arbitrary destination. We
# restore the binding by minting the ``channel`` prefill as an HMAC-signed token (bot-only secret);
# the webhook trusts routing ONLY when the token verifies. A hand-crafted/public submission yields no
# usable origin (the doc is still generated, just not auto-delivered to an unverified destination).
_ROUTING_KEY_CONTEXT = b"nda.tally.link.v1"


def _routing_key(secret: str) -> bytes:
    """A domain-separated sub-key so the routing HMAC can't be cross-used as a webhook signature."""
    return hmac.new(
        secret.encode("utf-8"), _ROUTING_KEY_CONTEXT, hashlib.sha256
    ).digest()


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def mint_routing_token(secret: str, channel: str, thread_ts: str = "") -> str:
    """The tamper-proof ``channel`` prefill the bot puts in the Tally link (``<b64(payload)>.<b64(sig)>``).

    Binds ``channel`` + ``thread_ts`` to a bot-issued HMAC so a respondent can't redirect the NDA by
    editing the URL. Paired with :func:`verify_routing_token` on the webhook side.
    """
    payload = json.dumps(
        {"c": channel, "t": thread_ts}, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    body = _b64u(payload)
    sig = _b64u(
        hmac.new(_routing_key(secret), body.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{body}.{sig}"


def verify_routing_token(secret: str, token: str | None) -> dict[str, str] | None:
    """``{channel, thread_ts}`` iff ``token`` carries a valid bot-issued signature, else ``None``.

    An unsigned/hand-crafted value (someone opening the public form URL directly) fails to verify and
    returns ``None`` — the caller then declines to auto-deliver to that unverified destination.
    """
    tok = (token or "").strip()
    if not secret or "." not in tok:
        return None
    body, _, sig = tok.partition(".")
    expected = _b64u(
        hmac.new(_routing_key(secret), body.encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    return {
        "channel": str(payload.get("c") or ""),
        "thread_ts": str(payload.get("t") or ""),
    }


# --------------------------------------------------------------------------- #
# Submission → tokens + routing + origin
# --------------------------------------------------------------------------- #
@dataclass
class TallyMapped:
    """The parsed submission: routing selectors + engine token values + requester routing.

    ``jurisdiction`` / ``counterparty_type`` / ``mutuality`` feed ``normalize_codes``; ``values`` is the
    ``{token: value}`` table (empties dropped) fed to ``fill_docx``; ``channel_raw`` / ``thread_ts`` are
    the hidden requester-routing fields turned into an envelope by :func:`origin_context`.
    """

    submission_id: str
    form_id: str
    jurisdiction: str = ""
    counterparty_type: str = ""
    mutuality: str = ""
    values: dict[str, str] = field(default_factory=dict)
    channel_raw: str = ""
    thread_ts: str = ""


def _resolve_value(fld: Mapping[str, Any]) -> Any:
    """Return a field's answer as a scalar string. Option fields (``value`` is an id list) resolve to
    the option text(s) via ``options``; plain fields pass through; lists join on ``, ``."""
    value = fld.get("value")
    ftype = fld.get("type")
    if ftype in _OPTION_TYPES and isinstance(value, list):
        by_id = {o.get("id"): o.get("text") for o in (fld.get("options") or [])}
        texts = [str(by_id.get(v, v)) for v in value if v is not None]
        return ", ".join(t for t in texts if t)
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v not in (None, ""))
    return value


def _first(value: Any) -> str:
    """First scalar of an option answer (already resolved to text by :func:`_resolve_value`)."""
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return "" if value is None else str(value)


def map_submission(payload: Mapping[str, Any]) -> TallyMapped:
    """Map a Tally ``FORM_RESPONSE`` webhook payload to :class:`TallyMapped`.

    Shape (Tally webhook): ``{eventType, data: {submissionId|responseId, formId, fields: [{key, label,
    type, value, options?}]}}``. Only the answered branch's fields carry values; routing dropdowns set
    the selectors; ``Description of services`` marks a ServiceProvider; the ``channel`` / ``thread_ts``
    hidden fields carry the requester origin. The raw payload is logged at debug so the live shape can
    be confirmed on the first real webhook.
    """
    data = dict(payload.get("data") or {})
    log.debug("tally.webhook.raw", event_type=payload.get("eventType"), data=data)

    submission_id = str(
        data.get("submissionId") or data.get("responseId") or data.get("id") or ""
    )
    form_id = str(data.get("formId") or "")
    fields = data.get("fields") or []

    jurisdiction = counterparty_type = mutuality = ""
    channel_raw = thread_ts = ""
    dropdown_purpose = ""
    is_service_provider = False
    values: dict[str, str] = {}

    for fld in fields:
        if not isinstance(fld, Mapping):
            continue
        label = fld.get("label")
        key = fld.get("key")
        ftype = str(fld.get("type") or "")
        n_label = _norm(label)
        n_key = _norm(key)

        # Hidden requester-routing fields (defensive: object value OR separate keyed fields).
        raw_value = fld.get("value")
        if isinstance(raw_value, Mapping) and raw_value.get("channel") is not None:
            channel_raw = str(raw_value.get("channel") or "")
            thread_ts = str(raw_value.get("thread_ts") or "")
            continue
        if n_label == "channel" or n_key == "channel":
            channel_raw = str(raw_value or "")
            continue
        if n_label == "thread_ts" or n_key == "thread_ts":
            thread_ts = str(raw_value or "")
            continue
        if ftype == "HIDDEN_FIELDS":
            continue

        value = _resolve_value(fld)

        # Routing selectors. The webhook sends EVERY branch's fields, with a null value for the
        # unanswered branches — and some labels (routing dropdowns, the per-branch purpose dropdown)
        # recur, so only overwrite from a NON-EMPTY value or an unanswered copy clobbers the real one.
        if n_label in _ROUTING:
            sel = _ROUTING[n_label]
            selected = _first(value)
            if selected:
                if sel == "jurisdiction":
                    jurisdiction = selected
                elif sel == "counterparty_type":
                    counterparty_type = selected
                elif sel == "mutuality":
                    mutuality = selected
            continue

        # The canned purpose dropdown (used only if no free-text purpose is present). Same recurring-
        # field guard: an unanswered branch's null purpose dropdown must NOT wipe the selected one.
        if n_label == "purpose of disclosure":
            chosen = _first(value)
            if chosen:
                dropdown_purpose = chosen
            continue
        # Skip the submit checkbox, the effective-date mode dropdown, and the id-type dropdown.
        if n_label == "ready to submit?" or ftype == "CHECKBOXES":
            continue
        if n_label == "effective date" and ftype == "DROPDOWN":
            continue
        if n_label == "nric / passport":
            continue

        if value in (None, "", "[]"):
            continue
        if n_label == "description of services":
            is_service_provider = True
        token = label_to_token(label, ftype)
        if token:
            values.setdefault(token, str(value))

    if dropdown_purpose and "purpose" not in values:
        values["purpose"] = dropdown_purpose

    cp = "service_provider" if is_service_provider else _norm(counterparty_type)
    return TallyMapped(
        submission_id=submission_id,
        form_id=form_id,
        jurisdiction=_first(jurisdiction).strip(),
        counterparty_type=cp,
        mutuality=_norm(mutuality),
        values=values,
        channel_raw=channel_raw,
        thread_ts=thread_ts,
    )


def origin_context(channel_raw: str, thread_ts: str = "") -> dict[str, str] | None:
    """Turn the hidden ``channel`` field into the ``envelope_context`` completion consumes.

    ``email||<addr>`` → an email origin (reply to ``<addr>``); a bare Slack conversation id (starts
    ``C``/``G``/``D``) → a Slack origin (threaded on ``thread_ts``). Returns ``None`` when there is no
    usable routing (an older submission with no pre-filled channel) — the doc is still generated and can
    be retrieved from the dashboard, but nothing is delivered conversationally.
    """
    ch = (channel_raw or "").strip()
    if not ch:
        return None
    if ch.lower().startswith("email||"):
        addr = ch.split("||", 1)[1].strip()
        if not addr:
            return None
        return {"channel": "email", "sender": addr}
    if ch[0] in ("C", "G", "D"):
        return {
            "channel": "slack",
            "slack_channel": ch,
            "slack_thread_ts": (thread_ts or "").strip(),
        }
    return None


__all__ = [
    "SIGNATURE_HEADER",
    "LABEL2TOKEN",
    "TallyMapped",
    "verify_signature",
    "mint_routing_token",
    "verify_routing_token",
    "map_submission",
    "origin_context",
    "label_to_token",
]
