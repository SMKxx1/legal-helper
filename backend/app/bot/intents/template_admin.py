"""The Slack guided template-replacement flow (PLAN §3.7 — the SIMPLE self-serve path).

Deep token work happens in the template *studio* (the admin web page: highlight→click tokenizer,
oplog undo/redo). This module is the SIMPLE Slack path a non-technical admin uses to swap a whole
template file for a corrected one without leaving the chat, exactly the four-step chain PLAN §3.7
names:

    "Update this template" (admin only) → upload the replacement .docx to the thread → validation
    reply (the studio checklist vs the registry's required set for that template — plain-English
    errors) → an optional sample-NDA *test drive* (dummy values) as the final human check →
    Confirm & publish (the SAME publish path the studio uses, including the drift emit) → the new
    version number + rollback info.

It is an **interactivity-driven chain from the template picker** (task deliverable 1), not a new
router intent:

* The picker card the ``template`` intent already posts is EXTENDED — for an admin sender only —
  with an *Update this template* button (:func:`admin_update_button_block`, added by
  :class:`AdminTemplateIntent`, which wraps the ported ``TemplateIntent`` in ``default_registry``).
  A non-admin never sees the button (task: "non-allowlisted sender gets no update button").
* Every subsequent step is a typed, versioned Slack button (``{v:1, kind, ref}``) whose ``ref`` keys
  a durable ``bot_correlation`` row holding the chain state (target template selectors + the recovered
  .docx bytes + the validation result). The handlers register onto the shared
  :class:`~app.bot.interactivity.InteractivityRegistry` via :func:`register_template_admin` — no change
  to :func:`~app.bot.interactivity.dispatch_interaction`.

Fail-closed authorization runs on EVERY click (PLAN §6, gates fail closed): a non-admin who somehow
fires a ``tpl_admin_*`` button is refused (task: "the chain refuses"). Admin identity mirrors the
approvals gate exactly — the click must originate in the configured admin Slack channel
(``NDA_ADMIN_SLACK_CHANNEL``), or the clicker must satisfy the optional injected ``is_admin`` predicate
(the seam an Entra-backed admin roster plugs into later).

Every collaborator is injected (a fake Slack file fetcher, a fake thread scanner, a fake publisher),
so the whole chain runs with **zero network** (PLAN house rules).
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import Field
from sqlalchemy import func, select, update

from ...telemetry import get_logger
from ..envelope import AttachmentRef, Envelope
from ..interactivity import (
    PAYLOAD_VERSION,
    ButtonPayload,
    Interaction,
    InteractivityDeps,
    InteractivityRegistry,
)
from ..models import BotCorrelation
from ..thread_docs import ThreadDoc, ThreadScanner
from . import IntentContext, IntentHandler, IntentReply
from .review import HttpSlackFileFetcher, SlackFileFetcher

if TYPE_CHECKING:
    from app.config import Settings
    from app.registry.drift import DriftNotifier

log = get_logger("nda.bot.intent.template_admin")

# --------------------------------------------------------------------------- #
# Kinds + action ids (self-describing; the dispatcher resolves the kind from the action id)
# --------------------------------------------------------------------------- #
KIND_TPL_ADMIN_UPDATE = "tpl_admin_update"
KIND_TPL_ADMIN_VALIDATE = "tpl_admin_validate"
KIND_TPL_ADMIN_TESTDRIVE = "tpl_admin_testdrive"
KIND_TPL_ADMIN_PUBLISH = "tpl_admin_publish"
KIND_TPL_ADMIN_CANCEL = "tpl_admin_cancel"

ACTION_TPL_ADMIN_UPDATE = "tpl_admin_update"
ACTION_TPL_ADMIN_VALIDATE = "tpl_admin_validate"
ACTION_TPL_ADMIN_TESTDRIVE = "tpl_admin_testdrive"
ACTION_TPL_ADMIN_PUBLISH = "tpl_admin_publish"
ACTION_TPL_ADMIN_CANCEL = "tpl_admin_cancel"

#: The bot_correlation kind for the template-admin chain state (distinct from the envelope kind).
TPL_ADMIN_CORRELATION_KIND = "tpl_admin"
#: How long a pending update chain lives before the worker sweep reaps it.
TPL_ADMIN_TTL_HOURS = 24.0

#: The variant the simple Slack flow replaces — the blank template the ``template`` intent hands out.
#: (Tokenised-variant token surgery is the studio's job, PLAN §3.7.)
UPDATE_VARIANT = "empty"

# --------------------------------------------------------------------------- #
# Preserved plain-English copy (PLAN §3.7 "plain-English errors")
# --------------------------------------------------------------------------- #
UPLOAD_ASK_TEXT = (
    "*Update the {combo} template*\n"
    "1. Upload the corrected `.docx` into this thread.\n"
    "2. Then click *Validate* below and I'll check its tokens before anything is published."
)
NO_SELECTORS_TEXT = (
    "Pick a *jurisdiction* and *counterparty type* in the picker above first (plus *mutuality* for an "
    "individual), then click *Update this template*."
)
NOT_ADMIN_TEXT = (
    "Only an admin can update templates. If you need a change, ask an admin to run it."
)
NO_DOC_TEXT = (
    "I couldn't find a `.docx` in this thread yet. Upload the replacement file here, then click "
    "*Validate* again."
)
DOC_FETCH_FAILED_TEXT = (
    "I found a file but couldn't download it — it may have been deleted. Re-upload the `.docx` and "
    "click *Validate* again."
)
EXPIRED_STATE_TEXT = (
    "This update has expired or was already finished. Start again from the template picker "
    "(*Update this template*)."
)
NOT_A_TEMPLATE_TEXT = (
    "There's no *{combo}* template loaded to update. Load it in the template studio first, then use "
    "this flow to replace it."
)
NOT_VALIDATED_TEXT = "Please click *Validate* first — I publish a replacement only after its tokens check out."
PUBLISH_BLOCKED_TEXT = (
    "I can't publish this yet: it's still missing required token(s): {missing}. Add them in Word, "
    "re-upload, and click *Validate* again."
)

TPL_ADMIN_FALLBACK_TEXT = "NDA template update — admin action."


# =========================================================================== #
# Typed, versioned button-value payloads (PLAN §3.3)
# =========================================================================== #
class TplAdminStartPayload(ButtonPayload):
    """The *Update this template* button value — carries no ref (the chain state is created on click;
    the target selectors are read from the picker's ``state.values``)."""

    kind: str = KIND_TPL_ADMIN_UPDATE


class TplAdminRefPayload(ButtonPayload):
    """Every downstream button (validate / test-drive / publish / cancel): ``{v:1, kind, ref}``.

    ``ref`` keys the ``bot_correlation`` row holding the chain state — the button's only secret, well
    under Slack's value-length limit (PLAN §3.9 "typed payloads carry only keys")."""

    kind: str
    ref: str = Field(min_length=1)


def _value_json(kind: str, **extra: Any) -> str:
    """The versioned, typed button VALUE JSON (PLAN §3.3): ``{"v":1,"kind":…,…}``."""
    return json.dumps(
        {"v": PAYLOAD_VERSION, "kind": kind, **extra}, separators=(",", ":")
    )


def _ref_value(kind: str, ref: str) -> str:
    return _value_json(kind, ref=ref)


def start_button_value() -> str:
    return _value_json(KIND_TPL_ADMIN_UPDATE)


# =========================================================================== #
# Block Kit builders (kept in this module so the whole flow is self-contained)
# =========================================================================== #
def _button(action_id: str, text: str, value: str, *, style: str | None = None) -> dict:
    btn: dict[str, Any] = {
        "type": "button",
        "action_id": action_id,
        "text": {"type": "plain_text", "text": text, "emoji": True},
        "value": value,
    }
    if style:
        btn["style"] = style
    return btn


def admin_update_button_block() -> dict:
    """The *Update this template* affordance appended to the picker card for an admin (task 1)."""
    return {
        "type": "actions",
        "elements": [
            _button(
                ACTION_TPL_ADMIN_UPDATE,
                "Update this template",
                start_button_value(),
            )
        ],
    }


def upload_ask_blocks(ref: str, combo: str) -> list[dict]:
    """The "upload the .docx, then Validate" ask + the Validate/Cancel buttons (step 1→2)."""
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": UPLOAD_ASK_TEXT.format(combo=combo)},
        },
        {
            "type": "actions",
            "elements": [
                _button(
                    ACTION_TPL_ADMIN_VALIDATE,
                    "Validate",
                    _ref_value(KIND_TPL_ADMIN_VALIDATE, ref),
                    style="primary",
                ),
                _button(
                    ACTION_TPL_ADMIN_CANCEL,
                    "Cancel",
                    _ref_value(KIND_TPL_ADMIN_CANCEL, ref),
                ),
            ],
        },
    ]


def validation_blocks(
    ref: str, summary_mrkdwn: str, *, publishable: bool
) -> list[dict]:
    """The validation reply: the plain-English checklist + (when publishable) Test drive / Confirm &
    publish, else just Re-validate."""
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary_mrkdwn}}
    ]
    if publishable:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    _button(
                        ACTION_TPL_ADMIN_TESTDRIVE,
                        "Test drive",
                        _ref_value(KIND_TPL_ADMIN_TESTDRIVE, ref),
                    ),
                    _button(
                        ACTION_TPL_ADMIN_PUBLISH,
                        "Confirm & publish",
                        _ref_value(KIND_TPL_ADMIN_PUBLISH, ref),
                        style="primary",
                    ),
                    _button(
                        ACTION_TPL_ADMIN_CANCEL,
                        "Cancel",
                        _ref_value(KIND_TPL_ADMIN_CANCEL, ref),
                    ),
                ],
            }
        )
    else:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    _button(
                        ACTION_TPL_ADMIN_VALIDATE,
                        "Re-validate",
                        _ref_value(KIND_TPL_ADMIN_VALIDATE, ref),
                    ),
                    _button(
                        ACTION_TPL_ADMIN_CANCEL,
                        "Cancel",
                        _ref_value(KIND_TPL_ADMIN_CANCEL, ref),
                    ),
                ],
            }
        )
    return blocks


def testdrive_blocks(ref: str) -> list[dict]:
    """After delivering the sample .docx, re-offer Confirm & publish (the sample is the final check)."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "Here's a *test-drive sample* filled with dummy values — open it and check it "
                    "looks right. When you're happy, click *Confirm & publish*."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                _button(
                    ACTION_TPL_ADMIN_PUBLISH,
                    "Confirm & publish",
                    _ref_value(KIND_TPL_ADMIN_PUBLISH, ref),
                    style="primary",
                ),
                _button(
                    ACTION_TPL_ADMIN_CANCEL,
                    "Cancel",
                    _ref_value(KIND_TPL_ADMIN_CANCEL, ref),
                ),
            ],
        },
    ]


# --------------------------------------------------------------------------- #
# Admin authorization (fail-closed; mirrors the approvals gate)
# --------------------------------------------------------------------------- #
def is_admin_sender(
    envelope: Envelope,
    settings: Settings | None,
    *,
    is_admin: Callable[[str], bool] | None = None,
) -> bool:
    """True iff ``envelope``'s sender is an admin (PLAN §3.7/§6). Fail-closed.

    Slack: the request originates in the configured admin channel (``NDA_ADMIN_SLACK_CHANNEL`` — the
    only Slack admin concept in this codebase, per the approvals gate's "no Slack env bypass" note) OR
    the (verified) ``sender_id`` satisfies the injected ``is_admin`` predicate. Email: a DMARC-aligned
    sender matching ``NDA_ADMIN_EMAIL``. Everything else is denied.
    """
    if settings is None:
        return False
    from app.settings_store import admin_routing

    admin_channel_raw, admin_email_raw = admin_routing(settings_obj=settings)
    if envelope.channel == "slack":
        sid = (envelope.sender_id or "").strip()
        if is_admin and sid and is_admin(sid):
            return True
        return bool(admin_channel_raw) and envelope.slack_channel == admin_channel_raw
    if envelope.channel == "email" and envelope.verified_sender:
        admin_email = admin_email_raw.lower()
        return (
            bool(admin_email)
            and (envelope.sender_address or "").strip().lower() == admin_email
        )
    return False


def _authorize_click(
    interaction: Interaction,
    settings: Settings | None,
    *,
    is_admin: Callable[[str], bool] | None,
) -> bool:
    """Fail-closed admin authorization for a ``tpl_admin_*`` click (PLAN §6). The click must originate in
    the admin channel OR the clicker must satisfy ``is_admin``. The clicker is always logged (audit)."""
    from app.settings_store import admin_routing

    admin_channel = admin_routing(settings_obj=settings)[0] if settings else ""
    from_admin_channel = bool(admin_channel) and interaction.channel_id == admin_channel
    by_predicate = bool(is_admin(interaction.clicker_id)) if is_admin else False
    authorized = from_admin_channel or by_predicate
    log.info(
        "bot.template_admin.authz",
        clicker=interaction.clicker_id,
        channel=interaction.channel_id,
        from_admin_channel=from_admin_channel,
        by_predicate=by_predicate,
        authorized=authorized,
    )
    return authorized


# --------------------------------------------------------------------------- #
# Correlation store (durable chain state behind the ref keys)
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(UTC)


def _store_state(session_factory: Any, payload: dict[str, Any]) -> str:
    from ...auth.security import new_token

    key = new_token(18)
    with session_factory() as session:
        session.add(
            BotCorrelation(
                key=key,
                kind=TPL_ADMIN_CORRELATION_KIND,
                payload_json=payload,
                expires_at=_now() + timedelta(hours=TPL_ADMIN_TTL_HOURS),
            )
        )
        session.commit()
    return key


def _load_state(session_factory: Any, ref: str) -> dict[str, Any] | None:
    if not ref or session_factory is None:
        return None
    try:
        with session_factory() as session:
            row = session.execute(
                select(BotCorrelation).where(BotCorrelation.key == ref)
            ).scalar_one_or_none()
            if row is None:
                return None
            exp = row.expires_at
            if exp is not None:
                exp = exp if exp.tzinfo else exp.replace(tzinfo=UTC)
                if exp < _now():
                    return None
            return dict(row.payload_json or {})
    except Exception as exc:  # noqa: BLE001 — a state read must never crash the ack path
        log.warning("bot.template_admin.state_read_failed", ref=ref, error=repr(exc))
        return None


def _update_state(session_factory: Any, ref: str, updates: dict[str, Any]) -> None:
    if not ref or session_factory is None:
        return
    try:
        with session_factory() as session:
            row = session.execute(
                select(BotCorrelation).where(BotCorrelation.key == ref)
            ).scalar_one_or_none()
            if row is None:
                return
            row.payload_json = {**(row.payload_json or {}), **updates}
            session.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("bot.template_admin.state_update_failed", ref=ref, error=repr(exc))


# --------------------------------------------------------------------------- #
# The publish path (the SAME blob+version+drift path the studio uses)
# --------------------------------------------------------------------------- #
class TemplateAdminError(Exception):
    """A publish precondition failed (e.g. no such template to replace). Turned into a friendly reply."""


@dataclass(frozen=True)
class PublishResult:
    """What a publish did — the new version number + rollback context surfaced back to the admin."""

    template_id: str
    variant: str
    version_no: int
    previous_version_no: int | None
    added_tokens: tuple[str, ...]
    removed_tokens: tuple[str, ...]


#: ``publisher(db, *, jurisdiction, counterparty, mutuality, variant, docx_bytes, notifier, actor)``.
TemplatePublisher = Callable[..., PublishResult]


def _resolve_template(db: Any, jur: str, cp: str, mut: str) -> Any | None:
    from app.models_v2 import Template

    return db.execute(
        select(Template).where(
            Template.jurisdiction_code == jur,
            Template.counterparty_type_code == cp,
            Template.mutuality_code == mut,
        )
    ).scalar_one_or_none()


def required_token_names(db: Any, template_id: str) -> list[str]:
    """The registry's required set for a template (PLAN §3.7): the tokens ``token_template`` maps to it
    (materialized from each token's scope). A replacement .docx must contain all of them to publish."""
    from app.models_v2 import Token, TokenTemplate

    rows = (
        db.execute(
            select(Token.name)
            .join(TokenTemplate, TokenTemplate.token_id == Token.id)
            .where(TokenTemplate.template_id == template_id)
            .order_by(Token.name)
        )
        .scalars()
        .all()
    )
    return list(rows)


def publish_template_version(
    db: Any,
    *,
    jurisdiction: str,
    counterparty: str,
    mutuality: str,
    variant: str,
    docx_bytes: bytes,
    notifier: DriftNotifier | None = None,
    actor: str = "",
) -> PublishResult:
    """Publish ``docx_bytes`` as the new current version of the (jurisdiction, counterparty, mutuality)
    template's ``variant`` — the SAME blob+version+drift path the studio publishes through (PLAN §3.7).

    Stores the bytes as a ``document_blob`` (dedup by sha256), inserts a ``template_version`` at
    ``max(version_no)+1`` with ``is_current=True`` (flipping the prior current off), and emits a
    ``template_published`` drift event carrying the added/removed token diff so every affected NDA form is
    flagged + its owner notified (§3.7). Raises :class:`TemplateAdminError` if the target template doesn't
    exist (this flow REPLACES an existing template; creating one is the studio's job).
    """
    from app.models_v2 import DocumentBlob, TemplateVersion
    from app.registry.drift import emit_template_published
    from app.studio.checklist import scan_token_names
    from app.support_task.generator import DOCX_MIME

    template = _resolve_template(db, jurisdiction, counterparty, mutuality)
    if template is None:
        raise TemplateAdminError(
            f"no template for {jurisdiction}/{counterparty}/{mutuality}"
        )

    # Prior token set (for the drift diff) + prior version number (for rollback context).
    prior_names: set[str] = set()
    previous_version_no: int | None = None
    current = db.execute(
        select(TemplateVersion).where(
            TemplateVersion.template_id == template.id,
            TemplateVersion.variant_code == variant,
            TemplateVersion.is_current.is_(True),
        )
    ).scalar_one_or_none()
    if current is not None:
        previous_version_no = current.version_no
        if current.blob_id:
            prior_blob = db.get(DocumentBlob, current.blob_id)
            if prior_blob is not None and prior_blob.bytes:
                prior_names = set(scan_token_names(prior_blob.bytes))

    # Blob (dedup identical bytes by sha256).
    sha = hashlib.sha256(docx_bytes).hexdigest()
    blob = db.execute(
        select(DocumentBlob).where(DocumentBlob.sha256 == sha)
    ).scalar_one_or_none()
    if blob is None:
        blob = DocumentBlob(
            sha256=sha,
            byte_size=len(docx_bytes),
            mime_type=DOCX_MIME,
            bytes=docx_bytes,
        )
        db.add(blob)
        db.flush()

    next_no = (
        int(
            db.execute(
                select(func.max(TemplateVersion.version_no)).where(
                    TemplateVersion.template_id == template.id,
                    TemplateVersion.variant_code == variant,
                )
            ).scalar()
            or 0
        )
        + 1
    )

    # Flip the prior current off, then insert the new current — the invariant one-current-per-variant.
    db.execute(
        update(TemplateVersion)
        .where(
            TemplateVersion.template_id == template.id,
            TemplateVersion.variant_code == variant,
            TemplateVersion.is_current.is_(True),
        )
        .values(is_current=False)
    )
    db.add(
        TemplateVersion(
            template_id=template.id,
            variant_code=variant,
            version_no=next_no,
            blob_id=blob.id,
            is_current=True,
            created_by=actor or None,  # attribution (P6): who published this version
        )
    )
    db.commit()

    new_names = set(scan_token_names(docx_bytes))
    added = tuple(sorted(new_names - prior_names))
    removed = tuple(sorted(prior_names - new_names))
    emit_template_published(
        db,
        template.id,
        added_tokens=added,
        removed_tokens=removed,
        notifier=notifier,
    )
    log.info(
        "bot.template_admin.published",
        template_id=template.id,
        variant=variant,
        version_no=next_no,
        previous_version_no=previous_version_no,
        added=list(added),
        removed=list(removed),
        actor=actor,
    )
    return PublishResult(
        template_id=template.id,
        variant=variant,
        version_no=next_no,
        previous_version_no=previous_version_no,
        added_tokens=added,
        removed_tokens=removed,
    )


# --------------------------------------------------------------------------- #
# Validation (studio checklist vs the registry's required set)
# --------------------------------------------------------------------------- #
def _combo_label(jur: str, cp: str, mut: str) -> str:
    label = {"Company": "Company", "ServiceProvider": "Service Provider"}.get(cp, cp)
    parts = [jur, label]
    if cp == "Individual" and mut in ("Mutual", "Unilateral"):
        parts.append(mut)
    return " / ".join(parts)


def validate_docx(
    db: Any, docx_bytes: bytes, jur: str, cp: str, mut: str
) -> dict[str, Any]:
    """Run the studio checklist for ``docx_bytes`` against the registry's required set for this template.

    Returns the ``analyze`` payload (``found`` / ``missing_required`` / ``unknown``). When the template
    doesn't exist the required set is empty (nothing to be missing) — the publish step surfaces the
    not-loaded error separately."""
    from app.registry.tokens import registry_token_names
    from app.studio.checklist import analyze

    template = _resolve_template(db, jur, cp, mut)
    required = required_token_names(db, template.id) if template is not None else []
    known = sorted(registry_token_names(db))
    return analyze(docx_bytes, required, known)


def validation_summary(result: dict[str, Any], combo: str) -> str:
    """Plain-English rendering of a checklist result for the Slack reply (PLAN §3.7)."""
    found = result.get("found") or []
    missing = result.get("missing_required") or []
    unknown = result.get("unknown") or []
    lines = [f"*Validation — {combo} template*"]
    lines.append(
        f":mag: Found *{len(found)}* token(s): "
        + (", ".join(f"`{t}`" for t in found) if found else "_none_")
    )
    if missing:
        lines.append(":x: Missing required: " + ", ".join(f"`{t}`" for t in missing))
    else:
        lines.append(":white_check_mark: All required tokens present.")
    if unknown:
        parts = []
        for item in unknown:
            name = item.get("name")
            close = item.get("closest_known")
            parts.append(f"`{name}`" + (f" (did you mean `{close}`?)" if close else ""))
        lines.append(":warning: Not in the registry: " + ", ".join(parts))
    return "\n".join(lines)


def _sample_values(db: Any, token_names: list[str]) -> dict[str, str]:
    """Type-aware dummy values for the test-drive fill (PLAN §3.7 sample-NDA test drive)."""
    from app.registry.tokens import get_token

    samples: dict[str, str] = {}
    for name in token_names:
        view = get_token(db, name)
        data_type = (view.data_type if view else "text") or "text"
        if data_type == "date":
            samples[name] = "1 January 2026"
        elif data_type == "email":
            samples[name] = "sample@example.com"
        else:
            label = (view.label if view else "") or name.replace("_", " ").title()
            samples[name] = f"[SAMPLE {label}]"
    return samples


# =========================================================================== #
# The admin picker wrapper (task 1: the button appears only for admins)
# =========================================================================== #
class AdminTemplateIntent:
    """Wraps the ported ``TemplateIntent`` and appends *Update this template* to the picker card when the
    sender is an admin (PLAN §3.7). A non-admin's reply is returned untouched — no button, so a
    non-allowlisted sender can never enter the update chain (task deliverable 4).

    The base intent's own behavior (the zero-row guard, the file delivery, the email ask) is unchanged —
    only the Slack picker card (``slack_blocks`` present) gains the extra button, and only for an admin.
    """

    def __init__(
        self,
        base: IntentHandler,
        *,
        settings: Settings | None = None,
        is_admin: Callable[[str], bool] | None = None,
    ) -> None:
        self._base = base
        self._settings = settings
        self._is_admin = is_admin

    def _get_settings(self) -> Settings | None:
        if self._settings is not None:
            return self._settings
        try:
            from app.config import get_settings

            return get_settings()
        except Exception:  # noqa: BLE001 — settings unavailable => no admin affordance (fail closed)
            return None

    def __call__(self, ctx: IntentContext) -> IntentReply:
        reply = self._base(ctx)
        # Only the interactive picker (incomplete selectors, Slack) is augmented; a file/text reply is not.
        if not reply.slack_blocks or ctx.envelope.channel != "slack":
            return reply
        if not is_admin_sender(
            ctx.envelope, self._get_settings(), is_admin=self._is_admin
        ):
            return reply
        log.info(
            "bot.template_admin.button_offered",
            event_key=ctx.envelope.event_key,
            slack_channel=ctx.envelope.slack_channel,
        )
        return replace(
            reply,
            slack_blocks=tuple(reply.slack_blocks) + (admin_update_button_block(),),
        )


def with_template_admin(
    base: IntentHandler,
    *,
    settings: Settings | None = None,
    is_admin: Callable[[str], bool] | None = None,
) -> IntentHandler:
    """Wrap ``base`` (the ported ``TemplateIntent``) so an admin's picker gains the update button."""
    return AdminTemplateIntent(base, settings=settings, is_admin=is_admin)


# =========================================================================== #
# Interactivity deps + the chain handlers
# =========================================================================== #
@dataclass(frozen=True)
class TemplateAdminDeps:
    """Template-admin collaborators, bound at :func:`register_template_admin` time (the default registry
    binds NONE — each is resolved lazily from the per-dispatch :class:`InteractivityDeps` + settings).

    Tests bind fakes here (a fake Slack file fetcher, a fake thread scanner, a fake publisher, an
    ``is_admin`` predicate) and pass an ordinary ``InteractivityDeps`` at dispatch — so the whole chain
    runs with zero network.
    """

    slack_fetch: SlackFileFetcher | None = None
    scanner: ThreadScanner | None = None
    publisher: TemplatePublisher | None = None
    is_admin: Callable[[str], bool] | None = None


class TemplateAdminInteractivity:
    """The ``tpl_admin_update`` / ``…_validate`` / ``…_testdrive`` / ``…_publish`` / ``…_cancel`` kind
    handlers (PLAN §3.7). Registered onto the shared :class:`InteractivityRegistry`."""

    def __init__(self, deps: TemplateAdminDeps | None = None) -> None:
        self._deps = deps or TemplateAdminDeps()

    # -- registration ------------------------------------------------------
    def register(self, registry: InteractivityRegistry) -> None:
        registry.register_action(ACTION_TPL_ADMIN_UPDATE, KIND_TPL_ADMIN_UPDATE)
        registry.register_kind(
            KIND_TPL_ADMIN_UPDATE,
            self._handle_start,
            value_model=TplAdminStartPayload,
        )
        for action_id, kind, handler in (
            (ACTION_TPL_ADMIN_VALIDATE, KIND_TPL_ADMIN_VALIDATE, self._handle_validate),
            (
                ACTION_TPL_ADMIN_TESTDRIVE,
                KIND_TPL_ADMIN_TESTDRIVE,
                self._handle_testdrive,
            ),
            (ACTION_TPL_ADMIN_PUBLISH, KIND_TPL_ADMIN_PUBLISH, self._handle_publish),
            (ACTION_TPL_ADMIN_CANCEL, KIND_TPL_ADMIN_CANCEL, self._handle_cancel),
        ):
            registry.register_action(action_id, kind)
            registry.register_kind(kind, handler, value_model=TplAdminRefPayload)

    # -- shared dep resolution --------------------------------------------
    def _get_slack_fetch(self, deps: InteractivityDeps) -> SlackFileFetcher:
        if self._deps.slack_fetch is not None:
            return self._deps.slack_fetch
        token = deps.settings.slack_bot_token if deps.settings else ""
        return HttpSlackFileFetcher(token)

    def _get_scanner(self, deps: InteractivityDeps) -> ThreadScanner:
        if self._deps.scanner is not None:
            return self._deps.scanner
        from ..thread_docs import HttpSlackThreadScanner

        token = deps.settings.slack_bot_token if deps.settings else ""
        return HttpSlackThreadScanner(token)

    def _authorized(self, interaction: Interaction, deps: InteractivityDeps) -> bool:
        return _authorize_click(
            interaction, deps.settings, is_admin=self._deps.is_admin or deps.is_admin
        )

    # -- step 1: start (read selectors, ask for the file) ------------------
    def _handle_start(self, interaction: Interaction, deps: InteractivityDeps) -> None:
        from ..blockkit import (
            ACTION_SELECT_COUNTERPARTY_TYPE,
            ACTION_SELECT_JURISDICTION,
            ACTION_SELECT_MUTUALITY,
        )
        from ..interactivity import _selected

        env = _interaction_envelope(interaction, deps)
        if not self._authorized(interaction, deps):
            _deliver_text(env, NOT_ADMIN_TEXT, deps)
            return

        jur_raw = _selected(interaction.state_values, ACTION_SELECT_JURISDICTION)
        cp_raw = _selected(interaction.state_values, ACTION_SELECT_COUNTERPARTY_TYPE)
        mut_raw = _selected(interaction.state_values, ACTION_SELECT_MUTUALITY)
        try:
            from app.support_task.generator import normalize_codes

            jur, cp, mut = normalize_codes(jur_raw, cp_raw, mut_raw)
        except Exception:  # noqa: BLE001 — a bad/empty combo is a friendly "pick selectors" reply
            _deliver_text(env, NO_SELECTORS_TEXT, deps)
            return

        combo = _combo_label(jur, cp, mut)
        ref = _store_state(
            deps.session_factory,
            {
                "jur": jur,
                "cp": cp,
                "mut": mut,
                "variant": UPDATE_VARIANT,
                "slack_channel": interaction.channel_id,
                "slack_thread_ts": interaction.thread_ts or interaction.message_ts,
                "combo": combo,
                "doc_b64": None,
                "validated": False,
                "missing_required": [],
                "started_by": interaction.clicker_id,
            },
        )
        log.info(
            "bot.template_admin.start",
            ref=ref,
            combo=combo,
            clicker=interaction.clicker_id,
        )
        _post_blocks(env, upload_ask_blocks(ref, combo), TPL_ADMIN_FALLBACK_TEXT, deps)

    # -- step 2: validate (recover the doc, run the checklist) -------------
    def _handle_validate(
        self, interaction: Interaction, deps: InteractivityDeps
    ) -> None:
        env = _interaction_envelope(interaction, deps)
        payload = interaction.payload
        if not isinstance(payload, TplAdminRefPayload):
            return
        if not self._authorized(interaction, deps):
            _deliver_text(env, NOT_ADMIN_TEXT, deps)
            return
        factory = deps.session_factory
        state = _load_state(factory, payload.ref)
        if state is None or factory is None:
            _deliver_text(env, EXPIRED_STATE_TEXT, deps)
            return

        docx_bytes, fetch_failed = self._recover_doc(interaction, state, deps)
        if fetch_failed:
            _deliver_text(env, DOC_FETCH_FAILED_TEXT, deps)
            return
        if docx_bytes is None:
            _deliver_text(env, NO_DOC_TEXT, deps)
            return

        jur, cp, mut = state["jur"], state["cp"], state["mut"]
        combo = str(state.get("combo") or _combo_label(jur, cp, mut))
        with factory() as db:
            result = validate_docx(db, docx_bytes, jur, cp, mut)
        missing = list(result.get("missing_required") or [])
        publishable = not missing
        _update_state(
            factory,
            payload.ref,
            {
                "doc_b64": base64.b64encode(docx_bytes).decode("ascii"),
                "validated": publishable,
                "missing_required": missing,
            },
        )
        log.info(
            "bot.template_admin.validated",
            ref=payload.ref,
            found=len(result.get("found") or []),
            missing=missing,
            publishable=publishable,
        )
        _post_blocks(
            env,
            validation_blocks(
                payload.ref,
                validation_summary(result, combo),
                publishable=publishable,
            ),
            TPL_ADMIN_FALLBACK_TEXT,
            deps,
        )

    # -- step 3 (optional): test drive (fill dummy values, deliver sample) -
    def _handle_testdrive(
        self, interaction: Interaction, deps: InteractivityDeps
    ) -> None:
        env = _interaction_envelope(interaction, deps)
        payload = interaction.payload
        if not isinstance(payload, TplAdminRefPayload):
            return
        if not self._authorized(interaction, deps):
            _deliver_text(env, NOT_ADMIN_TEXT, deps)
            return
        factory = deps.session_factory
        state = _load_state(factory, payload.ref)
        if state is None or factory is None or not state.get("doc_b64"):
            _deliver_text(env, EXPIRED_STATE_TEXT, deps)
            return
        docx_bytes = base64.b64decode(str(state["doc_b64"]))
        try:
            from app.studio.checklist import scan_token_names
            from app.support_task.generator import fill_docx

            with factory() as db:
                values = _sample_values(db, scan_token_names(docx_bytes))
            sample = fill_docx(docx_bytes, values, strip_unfilled=True)
        except Exception as exc:  # noqa: BLE001 — a fill failure is friendly, never a crash
            log.warning(
                "bot.template_admin.testdrive_failed",
                ref=payload.ref,
                error=repr(exc),
            )
            _deliver_text(
                env,
                "I couldn't build a test-drive sample from that file. You can still publish it, or "
                "re-upload a corrected `.docx` and click *Validate*.",
                deps,
            )
            return
        _deliver_sample(env, sample, deps)
        _post_blocks(env, testdrive_blocks(payload.ref), TPL_ADMIN_FALLBACK_TEXT, deps)

    # -- step 4: publish (same path as the studio, incl. the drift emit) ---
    def _handle_publish(
        self, interaction: Interaction, deps: InteractivityDeps
    ) -> None:
        env = _interaction_envelope(interaction, deps)
        payload = interaction.payload
        if not isinstance(payload, TplAdminRefPayload):
            return
        if not self._authorized(interaction, deps):
            _deliver_text(env, NOT_ADMIN_TEXT, deps)
            return
        factory = deps.session_factory
        state = _load_state(factory, payload.ref)
        if state is None or factory is None or not state.get("doc_b64"):
            _deliver_text(env, EXPIRED_STATE_TEXT, deps)
            return
        if not state.get("validated"):
            missing = list(state.get("missing_required") or [])
            if missing:
                _deliver_text(
                    env,
                    PUBLISH_BLOCKED_TEXT.format(
                        missing=", ".join(f"`{m}`" for m in missing)
                    ),
                    deps,
                )
            else:
                _deliver_text(env, NOT_VALIDATED_TEXT, deps)
            return

        docx_bytes = base64.b64decode(str(state["doc_b64"]))
        combo = str(
            state.get("combo") or _combo_label(state["jur"], state["cp"], state["mut"])
        )
        publisher = self._deps.publisher or publish_template_version
        notifier = self._build_notifier(deps)
        try:
            with factory() as db:
                result = publisher(
                    db,
                    jurisdiction=state["jur"],
                    counterparty=state["cp"],
                    mutuality=state["mut"],
                    variant=str(state.get("variant") or UPDATE_VARIANT),
                    docx_bytes=docx_bytes,
                    notifier=notifier,
                    # Namespace the Slack uploader so attribution distinguishes it from an admin user id.
                    actor=(
                        f"slack:{interaction.clicker_id}"
                        if interaction.clicker_id
                        else ""
                    ),
                )
        except TemplateAdminError:
            _deliver_text(env, NOT_A_TEMPLATE_TEXT.format(combo=combo), deps)
            return
        except Exception:  # noqa: BLE001 — a publish failure is reported, never a crash
            log.exception("bot.template_admin.publish_failed", ref=payload.ref)
            _deliver_text(
                env,
                "Something went wrong publishing that template — nothing was changed. Please try "
                "again shortly.",
                deps,
            )
            return

        # One-shot: expire the chain state so the Publish button can't double-publish.
        _update_state(factory, payload.ref, {"validated": False})
        _deliver_text(env, _published_text(result, combo), deps)

    # -- cancel ------------------------------------------------------------
    def _handle_cancel(self, interaction: Interaction, deps: InteractivityDeps) -> None:
        env = _interaction_envelope(interaction, deps)
        payload = interaction.payload
        if isinstance(payload, TplAdminRefPayload):
            _update_state(deps.session_factory, payload.ref, {"validated": False})
        _deliver_text(
            env,
            "Okay — I've cancelled this template update. Nothing was published.",
            deps,
        )

    # -- helpers -----------------------------------------------------------
    def _recover_doc(
        self, interaction: Interaction, state: dict[str, Any], deps: InteractivityDeps
    ) -> tuple[bytes | None, bool]:
        """Recover the uploaded .docx from the thread (reusing :mod:`app.bot.thread_docs`).

        Returns ``(bytes, fetch_failed)``: ``(bytes, False)`` on success, ``(None, False)`` when the
        thread carries no doc, and ``(None, True)`` when a doc was found but couldn't be downloaded —
        each maps to its own friendly reply."""
        channel = interaction.channel_id or str(state.get("slack_channel") or "")
        thread = (
            interaction.thread_ts
            or interaction.message_ts
            or str(state.get("slack_thread_ts") or "")
        )
        scanner = self._get_scanner(deps)
        try:
            doc: ThreadDoc | None = scanner(channel, thread)
        except Exception as exc:  # noqa: BLE001 — recovery is best-effort
            log.warning("bot.template_admin.scan_failed", error=repr(exc))
            return None, False
        if doc is None:
            return None, False
        att = AttachmentRef(
            filename=doc.file_name,
            source_ref=doc.file_id or doc.file_url,
        )
        try:
            return self._get_slack_fetch(deps)(att), False
        except Exception as exc:  # noqa: BLE001 — a fetch failure is a distinct friendly reply
            log.warning("bot.template_admin.fetch_failed", error=repr(exc))
            return None, True

    def _build_notifier(self, deps: InteractivityDeps) -> DriftNotifier | None:
        try:
            from app.registry.drift import DriftNotifier

            return DriftNotifier(service=deps.service)
        except Exception:  # noqa: BLE001 — drift notify is fail-soft; flagging still happens
            return None


def _published_text(result: PublishResult, combo: str) -> str:
    """The success reply: the new version number + rollback info (PLAN §3.7 versioning/rollback)."""
    lines = [
        f":white_check_mark: Published *{combo}* template — now *version {result.version_no}*."
    ]
    if result.added_tokens:
        lines.append(
            "Added token(s): " + ", ".join(f"`{t}`" for t in result.added_tokens)
        )
    if result.removed_tokens:
        lines.append(
            "Removed token(s): " + ", ".join(f"`{t}`" for t in result.removed_tokens)
        )
    if result.previous_version_no is not None:
        lines.append(
            f"_Rollback:_ version {result.previous_version_no} is retained — an admin can restore it "
            "from the template studio if needed."
        )
    if result.added_tokens or result.removed_tokens:
        lines.append(
            "_Any NDA forms affected by the token change have been flagged for a one-click sync._"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Reply/envelope helpers (self-contained, mirroring the envelope module)
# --------------------------------------------------------------------------- #
def _interaction_envelope(
    interaction: Interaction, deps: InteractivityDeps
) -> Envelope | None:
    channel = interaction.channel_id
    if not channel:
        return None
    thread = interaction.thread_ts or interaction.message_ts
    from_email = deps.settings.nda_bot_from_email if deps.settings else ""
    return Envelope(
        channel="slack",
        event_key=f"slack:int:tpladmin:{channel}:{thread or 'root'}",
        slack_channel=channel,
        slack_thread_ts=thread or "",
        verified_sender=True,
        from_email=from_email,
    )


def _deliver_text(env: Envelope | None, text: str, deps: InteractivityDeps) -> None:
    if env is None or deps.service is None:
        return
    from ..channels.protocol import Reply

    try:
        deps.service.deliver(env, Reply(text=text))
    except Exception as exc:  # noqa: BLE001 — delivery is fail-soft
        log.warning("bot.template_admin.deliver_failed", error=repr(exc))


def _deliver_sample(
    env: Envelope | None, docx_bytes: bytes, deps: InteractivityDeps
) -> None:
    if env is None or deps.service is None:
        return
    from app.support_task.generator import DOCX_MIME

    from ..channels.protocol import OutboundAttachment, Reply

    try:
        deps.service.deliver(
            env,
            Reply(
                text="Test-drive sample (dummy values):",
                attachments=(
                    OutboundAttachment(
                        filename="NDA-test-drive.docx",
                        content=docx_bytes,
                        content_type=DOCX_MIME,
                    ),
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001 — delivery is fail-soft
        log.warning("bot.template_admin.sample_deliver_failed", error=repr(exc))


def _post_blocks(
    env: Envelope | None, blocks: list[dict], fallback: str, deps: InteractivityDeps
) -> None:
    if env is None or deps.post_blocks is None:
        return
    try:
        deps.post_blocks(env, blocks, fallback)
    except Exception as exc:  # noqa: BLE001 — delivery is fail-soft
        log.warning("bot.template_admin.post_blocks_failed", error=repr(exc))


# --------------------------------------------------------------------------- #
# Registration seam (called from default_interactivity_registry)
# --------------------------------------------------------------------------- #
def register_template_admin(
    registry: InteractivityRegistry, *, deps: TemplateAdminDeps | None = None
) -> None:
    """Register the template-admin interactivity kinds onto ``registry`` (called from
    ``default_interactivity_registry``; tests pass ``deps`` to bind fakes)."""
    TemplateAdminInteractivity(deps).register(registry)


__all__ = [
    "AdminTemplateIntent",
    "with_template_admin",
    "TemplateAdminInteractivity",
    "TemplateAdminDeps",
    "TemplateAdminError",
    "PublishResult",
    "TemplatePublisher",
    "register_template_admin",
    "publish_template_version",
    "required_token_names",
    "validate_docx",
    "validation_summary",
    "is_admin_sender",
    "admin_update_button_block",
    "start_button_value",
    "KIND_TPL_ADMIN_UPDATE",
    "KIND_TPL_ADMIN_VALIDATE",
    "KIND_TPL_ADMIN_TESTDRIVE",
    "KIND_TPL_ADMIN_PUBLISH",
    "KIND_TPL_ADMIN_CANCEL",
    "ACTION_TPL_ADMIN_UPDATE",
    "TPL_ADMIN_CORRELATION_KIND",
    "UPDATE_VARIANT",
]
