"""The canonical normalized envelope — the ONE shape every intake path produces (PLAN §3.3).

Slack events (Bolt) and IMAP messages (worker poll) are each normalized into a single frozen
:class:`Envelope` before the router ever sees them, so the ported guard/dedup/routing logic maps 1:1
against the fields the n8n Router's ``Normalize (Slack)`` / ``Normalize (Email)`` Set nodes produced
(see the ground-truth reference §3.1). Keeping this a typed, immutable model — rather than a loose
dict — is what makes the guards, the allowlist gate, and the dedup key stable across the four builders
that consume it (Slack, email, router, worker).

Field parity with the n8n envelope (reference §2.1 "canonical context" + §3.1 Normalize nodes):

    n8n field            -> Envelope field
    channel              -> channel            ('slack' | 'email' — the only two)
    senderId             -> sender_id          (Slack user id / email From display)
    senderAddress        -> sender_address     (email address of the sender; '' on Slack)
    (new, PLAN §3.3)     -> verified_sender    (Slack: signature-verified; email: DMARC-aligned)
    slackChannel         -> slack_channel
    slackThreadTs        -> slack_thread_ts
    emailMessageId       -> email_message_id   (RFC Message-ID, for reply threading)
    emailSubject         -> email_subject
    text                 -> text               (cleaned body; quoted history already stripped)
    eventKey             -> event_key          ('slack:'+event_id / 'email:'+message-id — dedup key)
    files[]              -> attachments         (tuple of AttachmentRef)
    fromEmail            -> from_email          (the bot's own From address)
    (intake timestamp)   -> received_at

``verified_sender`` is the security-critical addition (PLAN §3.3, §6): Slack sets it once the v0 HMAC
signature verifies; email sets it only when SPF/DKIM/DMARC align. The allowlist and every
action-triggering intent (envelope, archive) gate on it — unverified senders are read-only-helpful.
The bytes of an attachment are NOT carried here; ``source_ref`` is the channel-specific handle a
handler uses to fetch them lazily (Slack file id/url, IMAP part reference).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: The only two intake channels in the whole system (reference §2.3 — an exact, case-sensitive match).
Channel = Literal["slack", "email"]


class AttachmentRef(BaseModel):
    """A reference to an inbound file — metadata only, never the bytes.

    ``source_ref`` is the opaque, channel-specific handle a handler resolves to bytes on demand: a
    Slack ``file id`` (or ``url_private_download``) or an IMAP part reference. Kept out of the hot
    path so the envelope stays small and cheap to persist in ``bot_inbox.payload_json``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    filename: str = ""
    content_type: str = ""
    size: int = 0
    source_ref: str = ""

    @field_validator("size")
    @classmethod
    def _non_negative_size(cls, v: int) -> int:
        return max(0, int(v))


class Envelope(BaseModel):
    """Normalized, immutable intake message — the router's sole input contract.

    Frozen so a handler can never mutate what it was dispatched (the guards, the dedup key, and the
    allowlist decision all read the same object). ``attachments`` is a tuple (a frozen model can't
    hold a mutable list) — pydantic coerces a passed ``list`` into one, so callers may build it from a
    list; iteration and ``len(...)`` behave identically to the n8n ``files[]`` array.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # ---- Identity & routing (both channels) ------------------------------
    channel: Channel
    #: Dedup + idempotency key. FAIL-CLOSED: it is the UNIQUE column on ``bot_inbox`` (PLAN §3.3), so
    #: it must never be blank — an empty key would collapse unrelated events into one dedup slot.
    event_key: str
    text: str = ""
    sender_id: str = ""
    sender_address: str = ""
    #: Slack: the v0 HMAC signature verified. Email: SPF/DKIM/DMARC aligned. The gate every
    #: allowlist / action-triggering intent checks (PLAN §3.3, §6) — default False = untrusted.
    verified_sender: bool = False

    # ---- Slack-specific --------------------------------------------------
    slack_channel: str = ""
    slack_thread_ts: str = ""

    # ---- Email-specific --------------------------------------------------
    email_message_id: str = ""
    email_subject: str = ""
    from_email: str = ""

    # ---- Attachments & timing -------------------------------------------
    attachments: tuple[AttachmentRef, ...] = ()
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("event_key")
    @classmethod
    def _event_key_required(cls, v: str) -> str:
        # Fail closed at construction: a blank dedup key is a normalization bug, not a runtime input.
        if not v or not v.strip():
            raise ValueError("event_key must be a non-empty dedup key")
        return v

    @property
    def has_content(self) -> bool:
        """The ported "Has Content?" guard predicate (reference §3.1): non-empty text OR any file.

        Provided as pure data derived from the envelope so the router (and the email/Slack intake) all
        apply the identical rule — an empty, attachment-less message is dropped.
        """
        return bool(self.text.strip()) or bool(self.attachments)
