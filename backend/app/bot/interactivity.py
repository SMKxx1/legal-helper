"""Typed, versioned Slack interactivity payloads + their handlers (PLAN §3.3 step 6, reference §3.7).

The in-process replacement for the n8n ``NDA: Interactivity`` webhook's ``Parse Template Selection`` +
``Route Interactivity`` Switch (reference §3.7). Where the old system passed context through a Slack
button ``value`` blob that a downstream bundle node then read from a *nonexistent* ``Context`` node —
so button/file replies lost their thread (reference §9 "Gaps" #2, the lost-context bug) — every inbound
interaction is now parsed into a schema-validated, versioned typed payload, and every reply is delivered
against a channel/thread reconstructed from that payload (or from a durable ``bot_correlation`` row).

Two layers of identification, mirroring the ground-truth router:

* **Interaction identity** — the ``action_id`` (block_actions) or ``callback_id`` (view_submission)
  resolves to a *kind* via the :class:`InteractivityRegistry`. This is the extension seam: P3 kinds
  (``send_docusign`` modal-open, ``env_use_doc``, ``arch_use_doc``, ``decline_doc``, the ``nda_docusign``
  modal submit) register a new ``(action_id|callback_id) -> kind -> handler`` triple and drop in without
  touching :func:`dispatch_interaction`.
* **Typed payload** — a button that carries state ships a versioned JSON value
  ``{v: 1, kind: str, …}`` (PLAN §3.3). :func:`dispatch_interaction` validates it against the kind's
  pydantic model BEFORE the handler runs; an unknown ``kind``/``v`` or a malformed value degrades to a
  logged, friendly "this button expired" reply instead of a stack trace.

Kinds this wave:

* ``template_picker`` — the wave-A picker's *Get template* submit (``template_submit``). It has no button
  value; the three selections are read from the Slack ``state.values`` by their preserved ``action_id``s
  (reference §3.2). The origin thread is reconstructed from the interaction payload (and pinned by a
  ``bot_correlation`` row when the template intent stored one keyed by the picker message ts), then a
  ``template`` :class:`~app.bot.router.Classification` is dispatched to the ``template`` intent handler so
  the file lands in the RIGHT thread. The three static-select changes route to a benign ``ignore`` no-op.
* ``approval`` — the admin *Approve* / *Deny* buttons (reference §3.4). Fail-closed authorization (the
  click must originate in the admin channel, or the clicker must be a configured admin) → call
  ``app.bot.approvals.approve_request`` / ``deny_request`` with the CLICKER as approver → post the result
  in the admin thread → notify the original requester in THEIR thread (carried in the typed payload, with
  a ``bot_correlation`` fallback). Idempotent: a second click sees ``ok=False`` and reports "already
  handled" without re-notifying.

Everything is fail-soft (the Bolt route already ACKed <3s before this runs): a handler error is logged,
never raised. Delivery goes through the same channel-aware :class:`~app.bot.channels.replies.ReplyService`
the router uses (resolved from :func:`app.bot.router.configure_delivery` when not injected).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select

from ..config import Settings, get_settings
from ..telemetry import get_logger
from .blockkit import (
    ACTION_SELECT_COUNTERPARTY_TYPE,
    ACTION_SELECT_JURISDICTION,
    ACTION_SELECT_MUTUALITY,
    ACTION_TEMPLATE_SUBMIT,
)
from .channels.protocol import Reply
from .envelope import Envelope
from .intents import IntentContext, IntentRegistry, default_registry
from .models import BotCorrelation, NdaPendingRequest
from .router import Classification

if TYPE_CHECKING:
    from sqlalchemy.orm import sessionmaker

log = get_logger("nda.bot.interactivity")

#: A zero-arg factory yielding a SQLAlchemy ``Session`` (a ``sessionmaker`` satisfies this).
SessionFactory = Callable[[], Any]

# --------------------------------------------------------------------------- #
# Kinds + preserved copy
# --------------------------------------------------------------------------- #
KIND_TEMPLATE_PICKER = "template_picker"
KIND_APPROVAL = "approval"
#: The requester's *Request approval* confirm button (PLAN §3.4): the click that actually pings the admin.
KIND_REQUEST_APPROVAL = "request_approval"
#: A known-but-benign interaction (the picker's static-select changes) — acknowledged, never replied to.
KIND_IGNORE = "ignore"

#: The friendly "expired" reply for an unknown kind / bad version / malformed value / stale button
#: (PLAN §3.3 step 6). Mrkdwn, delivered into the interaction's own thread.
EXPIRED_TEXT = (
    "Sorry — that button has expired or is no longer valid. Please start again by "
    "mentioning me with what you need."
)
#: Shown when a ``template_submit`` fires but the ``template`` intent isn't wired (degrade, don't crash).
TEMPLATE_UNAVAILABLE_TEXT = (
    "Sorry — template delivery isn't available right now. Please try again in a moment."
)

#: The bot_correlation key namespaces this module reads (the template intent / approvals gate write
#: them; here we consume them). Kept as helpers so the string convention lives in exactly one place.


def _template_correlation_key(picker_message_ts: str) -> str:
    return f"template_picker:{picker_message_ts}"


def _approval_correlation_key(request_key: str) -> str:
    return f"approval:{request_key}"


# =========================================================================== #
# Typed, versioned button-value payloads (PLAN §3.3)
# =========================================================================== #
PAYLOAD_VERSION = 1


class InteractivityError(Exception):
    """A parse/validation failure on an inbound interaction — the dispatcher turns it into the friendly
    "expired" reply. Distinct from a handler runtime error (logged, swallowed) so the two paths differ."""


class ButtonPayload(BaseModel):
    """Base for every typed Slack button VALUE (PLAN §3.3): a version pin + a kind discriminator.

    ``extra='ignore'`` tolerates same-version fields a future producer might add (forward compatibility)
    while an unsupported ``v`` is rejected outright — a stale button from before a schema bump degrades
    to the friendly "expired" reply rather than being mis-parsed.
    """

    model_config = ConfigDict(extra="ignore")

    v: int
    kind: str

    @field_validator("v")
    @classmethod
    def _supported_version(cls, value: int) -> int:
        if value != PAYLOAD_VERSION:
            raise ValueError(
                f"unsupported payload version {value!r} (this build speaks v{PAYLOAD_VERSION})"
            )
        return value


class ApprovalPayload(ButtonPayload):
    """The Approve/Deny button value — the exact shape the approvals agent's ``admin_notice_blocks``
    posts (``app.bot.approvals.approval_button_value`` → ``{v:1, kind:"approval", request_key, action}``).

    ``request_key`` is the idempotent pending-request handle; ``action`` (``"approve"``/``"deny"``) is the
    producer's authoritative decision (this handler prefers it, falling back to the ``action_id``). The
    optional ``requester_*`` fields let a future producer carry the requester's delivery context inline
    (the durable bridge across the admin's click context, PLAN §3.3); when absent — as in the current
    producer — the requester is resolved from the persisted ``NdaPendingRequest`` row instead (its stored
    ``requester`` + ``channel``), so the decision still reaches the original requester.
    """

    kind: Literal["approval"] = "approval"
    request_key: str = Field(min_length=1)
    #: The producer's decision: 'approve' | 'deny' (authoritative; action_id is the fallback).
    action: str = ""
    intent: str = ""
    #: Slack channel/user id OR email address of the requester.
    requester_channel_kind: str = ""
    requester_channel: str = ""
    requester_thread_ts: str = ""
    requester_id: str = ""
    requester_subject: str = ""


class RequestApprovalPayload(ButtonPayload):
    """The requester's *Request approval* confirm-button value (PLAN §3.4 — the confirm-before-request
    step). Carries only the idempotent ``request_key``; the requester identity is the CLICKER (verified
    fail-closed against the persisted row's stored requester), never a value the button carries."""

    kind: Literal["request_approval"] = "request_approval"
    request_key: str = Field(min_length=1)


# =========================================================================== #
# The parsed interaction + the extensible registry
# =========================================================================== #
@dataclass(frozen=True)
class Interaction:
    """A normalized, channel-decoded view of one Slack interactivity body — a handler's typed input.

    ``payload`` is the schema-validated button value (``None`` for kinds with no value, e.g. the picker);
    ``state_values`` is the Slack view/block ``state.values`` (the picker's static-select selections).
    ``channel_id`` / ``thread_ts`` are the interaction's OWN context (for block_actions: the message the
    button lives on) — for approvals that is the admin channel; the requester's context lives in the
    payload instead.
    """

    kind: str
    type: str
    clicker_id: str = ""
    action_id: str = ""
    callback_id: str = ""
    channel_id: str = ""
    thread_ts: str = ""
    message_ts: str = ""
    trigger_id: str = ""
    response_url: str = ""
    payload: BaseModel | None = None
    state_values: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


#: A kind handler: ``(interaction, deps) -> None``. Fail-soft — a raised error is logged by the dispatcher.
InteractionHandler = Callable[["Interaction", "InteractivityDeps"], None]


@dataclass(frozen=True)
class _Registration:
    handler: InteractionHandler
    value_model: type[BaseModel] | None = None


class InteractivityRegistry:
    """Maps ``action_id`` / ``callback_id`` → kind, and kind → (handler, optional value model).

    The extension seam PLAN §3.3 calls for: a P3 kind registers its action/callback ids and a handler
    here and is dispatched with no change to :func:`dispatch_interaction`. Registration is last-wins.
    """

    def __init__(self) -> None:
        self._action_kinds: dict[str, str] = {}
        self._callback_kinds: dict[str, str] = {}
        self._registry: dict[str, _Registration] = {}

    def register_action(self, action_id: str, kind: str) -> None:
        self._action_kinds[action_id] = kind

    def register_callback(self, callback_id: str, kind: str) -> None:
        self._callback_kinds[callback_id] = kind

    def register_kind(
        self,
        kind: str,
        handler: InteractionHandler,
        *,
        value_model: type[BaseModel] | None = None,
    ) -> None:
        self._registry[kind] = _Registration(handler=handler, value_model=value_model)

    def kind_for_action(self, action_id: str) -> str | None:
        return self._action_kinds.get(action_id)

    def kind_for_callback(self, callback_id: str) -> str | None:
        return self._callback_kinds.get(callback_id)

    def registration(self, kind: str) -> _Registration | None:
        return self._registry.get(kind)


def default_interactivity_registry() -> InteractivityRegistry:
    """The registry the dispatcher uses by default: template picker + approvals (this wave)."""
    reg = InteractivityRegistry()

    # Template picker (reference §3.2 / §3.7): the submit fires the template intent; the three
    # static-select CHANGES fire block_actions too and must be silently ignored (the n8n "ignore" NoOp).
    reg.register_action(ACTION_TEMPLATE_SUBMIT, KIND_TEMPLATE_PICKER)
    reg.register_kind(KIND_TEMPLATE_PICKER, _handle_template_picker)
    for select_action in (
        ACTION_SELECT_JURISDICTION,
        ACTION_SELECT_COUNTERPARTY_TYPE,
        ACTION_SELECT_MUTUALITY,
    ):
        reg.register_action(select_action, KIND_IGNORE)
    # The generate flow's "Open the NDA form" URL button (reference §3.7: the ported open_tally link):
    # a LINK button still POSTs a block_actions interaction on click, and it must be IGNORED — else the
    # dispatcher posts "that button has expired" into the thread on the generate happy path every time.
    from .intents.generate import ACTION_OPEN_FORM

    reg.register_action(ACTION_OPEN_FORM, KIND_IGNORE)
    reg.register_kind(KIND_IGNORE, _handle_ignore)

    # Approvals (reference §3.4): both of the approvals agent's buttons route to the one approval handler;
    # the value is validated against ApprovalPayload. The action_ids are OWNED by ``app.bot.approvals``
    # (it posts the card) — resolved here so a rename there surfaces immediately instead of mis-routing.
    approve_id, deny_id = _approval_action_ids()
    reg.register_action(approve_id, KIND_APPROVAL)
    reg.register_action(deny_id, KIND_APPROVAL)
    reg.register_kind(KIND_APPROVAL, _handle_approval, value_model=ApprovalPayload)

    # The requester's *Request approval* confirm button (PLAN §3.4): the ONLY click that pings the admin.
    reg.register_action(_request_approval_action_id(), KIND_REQUEST_APPROVAL)
    reg.register_kind(
        KIND_REQUEST_APPROVAL,
        _handle_request_approval,
        value_model=RequestApprovalPayload,
    )

    # Envelope / DocuSign kinds (PLAN §3.9): send_docusign modal-open, the nda_docusign modal submit,
    # the confirm card send/cancel, and the env_use_doc / decline_doc thread-doc chain. Registered from
    # the envelope module via its own seam (imported lazily to avoid an import cycle — envelope.py imports
    # the Interaction/registry types from here). A missing/broken module never breaks the base registry.
    try:
        from .intents.envelope import register_envelope

        register_envelope(reg)
    except Exception:  # noqa: BLE001 — envelope kinds are additive; base registry stays usable without them
        log.warning("bot.interactivity.envelope_register_failed")

    # Archive kind (PLAN §3.10): the arch_use_doc thread-doc confirm. Registered the same way envelope
    # is — from its own module seam, lazily, additive. Its "No, attach a file" button reuses the shared
    # decline_doc kind already registered by register_envelope above (identical reply), so only
    # arch_use_doc is added here. A missing/broken module never breaks the base registry.
    try:
        from .intents.archive import register_archive

        register_archive(reg)
    except Exception:  # noqa: BLE001 — archive kind is additive; base registry stays usable without it
        log.warning("bot.interactivity.archive_register_failed")

    # Template-admin kinds (PLAN §3.7): the Slack guided template-replacement chain
    # (tpl_admin_update -> validate -> testdrive -> publish -> cancel). Registered from its own module
    # seam, lazily + additive, exactly like envelope/archive. A missing/broken module never breaks the
    # base registry.
    try:
        from .intents.template_admin import register_template_admin

        register_template_admin(reg)
    except Exception:  # noqa: BLE001 — template-admin kinds are additive; base registry stays usable without them
        log.warning("bot.interactivity.template_admin_register_failed")
    return reg


def _approval_action_ids() -> tuple[str, str]:
    """The (approve, deny) action_ids the approvals agent posts. Resolved lazily from ``app.bot.approvals``
    (the producer/owner of the approval card) with a mirror of the known contract as a fallback, so this
    module never fails to import when approvals is briefly absent/broken."""
    try:
        from .approvals import ACTION_APPROVAL_APPROVE, ACTION_APPROVAL_DENY

        return ACTION_APPROVAL_APPROVE, ACTION_APPROVAL_DENY
    except Exception:  # noqa: BLE001 - fall back to the known contract strings (kept in one place)
        return "approval_approve", "approval_deny"


def _request_approval_action_id() -> str:
    """The requester *Request approval* button action_id (owned by ``app.bot.approvals``)."""
    try:
        from .approvals import ACTION_REQUEST_APPROVAL

        return ACTION_REQUEST_APPROVAL
    except Exception:  # noqa: BLE001 — fall back to the known contract string
        return "request_approval"


# =========================================================================== #
# Dependencies + resolution
# =========================================================================== #
@dataclass(frozen=True)
class InteractivityDeps:
    """Everything the kind handlers need, injectable for tests (zero network) and resolvable in prod.

    Any field left ``None`` is filled by :func:`_effective_deps` from the process-wide configuration
    (the reply service from :func:`app.bot.router.configure_delivery`, settings from
    :func:`app.config.get_settings`, the intent registry from :func:`app.bot.intents.default_registry`,
    the approvals module lazily from ``app.bot.approvals``). ``is_admin`` is an OPTIONAL predicate the
    approvals agent (or P3) can supply to authorize an admin identity outside the admin channel.
    """

    session_factory: SessionFactory | None = None
    #: A channel-aware ``ReplyService`` (its ``.deliver(envelope, Reply)``); ``None`` => replies dropped.
    service: Any | None = None
    #: ``SlackReplySink.post_blocks`` for interactive Block Kit cards (Slack only).
    post_blocks: Any | None = None
    settings: Settings | None = None
    #: The ``app.bot.approvals`` module (or any object exposing ``approve_request`` / ``deny_request``).
    approvals: Any | None = None
    intent_registry: IntentRegistry | None = None
    #: Optional admin-identity predicate ``(clicker_id) -> bool``.
    is_admin: Callable[[str], bool] | None = None


def _effective_deps(deps: InteractivityDeps | None) -> InteractivityDeps:
    """Fill any unset dep from the process-wide configuration (leaving injected values untouched)."""
    deps = deps or InteractivityDeps()
    service, post_blocks = deps.service, deps.post_blocks
    if service is None:
        wired = _process_wide_delivery()
        if wired is not None:
            service, post_blocks = wired
    return replace(
        deps,
        service=service,
        post_blocks=post_blocks,
        settings=deps.settings or get_settings(),
        approvals=(deps.approvals if deps.approvals is not None else _load_approvals()),
        intent_registry=deps.intent_registry or default_registry(),
        session_factory=deps.session_factory or _default_session_factory(),
    )


def _process_wide_delivery() -> tuple[Any, Any] | None:
    """Read the reply service the router was configured with (:func:`app.bot.router.configure_delivery`).

    Interactivity delivers through the SAME channel-aware service the routing pipeline uses; accessing
    the router's process-wide config keeps a single source of truth for "how does this process talk back
    to Slack/email"."""
    from . import router

    return router._DELIVERY


def _load_approvals() -> Any | None:
    """Import ``app.bot.approvals`` lazily (the approvals agent lands it alongside this module).

    ``None`` when the module isn't present yet — the approval handler then degrades to a friendly "not
    wired up" reply instead of crashing, mirroring the router's ``_load_router`` tolerance."""
    try:
        from . import approvals
    except Exception:  # noqa: BLE001 - module genuinely absent is an expected interleaved-build state
        return None
    return approvals


def _default_session_factory() -> sessionmaker:
    from app.db import SessionLocal

    return SessionLocal


# =========================================================================== #
# The dispatcher
# =========================================================================== #
def dispatch_interaction(
    body: dict[str, Any],
    *,
    registry: InteractivityRegistry | None = None,
    deps: InteractivityDeps | None = None,
) -> None:
    """Parse → validate → route ONE Slack interactivity body (block_actions / view_submission).

    Resolves the kind from the action/callback id, validates the typed button value against the kind's
    model (a bad version/kind/JSON → friendly "expired" reply), then invokes the kind handler. Never
    raises — the Bolt route already ACKed, so a handler failure is logged and swallowed (reference §3.7,
    all risky nodes → Flow Error Reply). An unregistered action/callback is treated as a stale button.
    """
    registry = registry or default_interactivity_registry()
    deps = _effective_deps(deps)

    itype = body.get("type", "") or ""
    clicker = _dig(body, "user", "id") or ""

    if itype == "block_actions":
        actions = body.get("actions") or []
        act = actions[0] if actions else {}
        action_id = act.get("action_id", "") or ""
        kind = registry.kind_for_action(action_id)
        interaction = Interaction(
            kind=kind or "",
            type=itype,
            action_id=action_id,
            clicker_id=clicker,
            channel_id=_dig(body, "channel", "id")
            or _dig(body, "container", "channel_id")
            or "",
            thread_ts=_dig(body, "message", "thread_ts")
            or _dig(body, "container", "thread_ts")
            or _dig(body, "message", "ts")
            or _dig(body, "container", "message_ts")
            or "",
            message_ts=_dig(body, "message", "ts")
            or _dig(body, "container", "message_ts")
            or "",
            trigger_id=body.get("trigger_id", "") or "",
            response_url=body.get("response_url", "") or "",
            state_values=_dig(body, "state", "values") or {},
            raw=body,
        )
        raw_value = act.get("value")
    elif itype == "view_submission":
        callback_id = _dig(body, "view", "callback_id") or ""
        kind = registry.kind_for_callback(callback_id)
        interaction = Interaction(
            kind=kind or "",
            type=itype,
            callback_id=callback_id,
            clicker_id=clicker,
            trigger_id=body.get("trigger_id", "") or "",
            state_values=_dig(body, "view", "state", "values") or {},
            raw=body,
        )
        # View submissions carry state in private_metadata (a P3 concern), never a button value.
        raw_value = None
    else:
        log.info("bot.interactivity.unhandled_type", type=itype)
        return None

    log.info(
        "bot.interactivity.received",
        type=itype,
        kind=kind,
        action_id=interaction.action_id,
        callback_id=interaction.callback_id,
        clicker=clicker,
    )

    if kind is None:
        log.warning(
            "bot.interactivity.unknown",
            type=itype,
            action_id=interaction.action_id,
            callback_id=interaction.callback_id,
        )
        _reply_expired(interaction, deps)
        return None

    reg = registry.registration(kind)
    if reg is None:
        log.warning("bot.interactivity.no_handler", kind=kind)
        _reply_expired(interaction, deps)
        return None

    if reg.value_model is not None:
        try:
            payload = _parse_payload(raw_value, reg.value_model)
        except InteractivityError as exc:
            log.warning("bot.interactivity.payload_invalid", kind=kind, error=str(exc))
            _reply_expired(interaction, deps)
            return None
        interaction = replace(interaction, payload=payload)

    try:
        reg.handler(interaction, deps)
    except Exception:  # noqa: BLE001 - fail-soft: the ack is already sent, never crash the route
        log.exception("bot.interactivity.handler_error", kind=kind)
    return None


def _parse_payload(raw_value: Any, model: type[BaseModel]) -> BaseModel:
    """Decode a Slack button value (a JSON string) and validate it against ``model``.

    Raises :class:`InteractivityError` on any failure (missing value, non-JSON, wrong shape, unsupported
    version) so the caller can render the single friendly "expired" reply."""
    if raw_value is None:
        raise InteractivityError("button carried no value")
    if isinstance(raw_value, str):
        try:
            data = json.loads(raw_value)
        except (ValueError, TypeError) as exc:
            raise InteractivityError(f"button value is not JSON: {exc}") from exc
    elif isinstance(raw_value, dict):
        data = raw_value
    else:
        raise InteractivityError(
            f"unexpected button value type {type(raw_value).__name__}"
        )
    if not isinstance(data, dict):
        raise InteractivityError("button value must be a JSON object")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise InteractivityError(f"payload failed validation: {exc}") from exc


# =========================================================================== #
# Handlers
# =========================================================================== #
def _handle_ignore(interaction: Interaction, deps: InteractivityDeps) -> None:
    """A benign, acknowledged no-op — the picker's static-select changes (reference §3.7 "ignore")."""
    log.debug("bot.interactivity.ignore", action_id=interaction.action_id)


def _handle_template_picker(interaction: Interaction, deps: InteractivityDeps) -> None:
    """``template_submit``: read the three selections from Slack state, dispatch the ``template`` intent
    into the ORIGIN thread (reference §3.2/§3.7).

    The lost-context fix (PLAN §3.3): the origin channel/thread is reconstructed from the interaction
    payload, and PINNED by a ``bot_correlation`` row keyed by the picker message ts when the template
    intent stored one — so the template file always lands where the request was made, never in an empty
    default context.
    """
    jurisdiction = _selected(interaction.state_values, ACTION_SELECT_JURISDICTION)
    counterparty_type = _selected(
        interaction.state_values, ACTION_SELECT_COUNTERPARTY_TYPE
    )
    mutuality = _selected(interaction.state_values, ACTION_SELECT_MUTUALITY)

    channel_id, thread_ts = _template_origin(interaction, deps)
    envelope = _slack_envelope(channel_id, thread_ts, deps, tag="template")
    if envelope is None:
        log.warning("bot.interactivity.template.no_channel")
        return

    classification = Classification(
        intent="template",
        jurisdiction=jurisdiction,
        counterparty_type=counterparty_type,
        mutuality=mutuality,
    )
    registry = deps.intent_registry or default_registry()
    handler = registry.get("template")
    log.info(
        "bot.interactivity.template_submit",
        event_key=envelope.event_key,
        jurisdiction=jurisdiction,
        counterparty_type=counterparty_type,
        mutuality=mutuality,
        slack_channel=channel_id,
        thread_ts=thread_ts,
        has_template_handler=handler is not None,
    )
    if handler is None:
        _deliver_text(envelope, TEMPLATE_UNAVAILABLE_TEXT, deps)
        return
    reply = handler(IntentContext(envelope=envelope, classification=classification))
    _deliver_intent_reply(envelope, reply, deps)


def _template_origin(
    interaction: Interaction, deps: InteractivityDeps
) -> tuple[str, str]:
    """Resolve the ORIGIN (channel, thread) for the template file.

    Prefers a ``bot_correlation`` row keyed by the picker message ts (the template intent stores the
    origin envelope there); otherwise reconstructs from the interaction payload (which for a threaded
    picker already carries the origin thread) — either way, never the empty context of the old bug.
    """
    if interaction.message_ts and deps.session_factory is not None:
        stored = _load_correlation(
            deps.session_factory,
            _template_correlation_key(interaction.message_ts),
            KIND_TEMPLATE_PICKER,
        )
        if stored:
            channel = str(stored.get("slack_channel") or stored.get("channel") or "")
            thread = str(stored.get("slack_thread_ts") or stored.get("thread") or "")
            if channel:
                return channel, thread or interaction.thread_ts
    return interaction.channel_id, interaction.thread_ts


def _handle_approval(interaction: Interaction, deps: InteractivityDeps) -> None:
    """``approve``/``deny`` click (reference §3.4): authorize, apply, report, notify the requester.

    Authorization is fail-closed (PLAN §6, gates fail closed): the click must originate in the admin
    channel OR the clicker must be a configured admin (``is_admin``). The CLICKER is the approver. The
    result is posted in the admin thread; the original requester is notified from the persisted
    ``NdaPendingRequest`` row (its stored ``requester`` + ``channel``) — the ported DM-to-requester
    behavior. Idempotent at THIS layer: the requester is notified only on the click that actually
    transitions the row (``pending -> approved/denied``), so a double-click never re-notifies.
    """
    payload = interaction.payload
    if not isinstance(
        payload, ApprovalPayload
    ):  # dispatcher guarantees this; defence in depth
        log.warning("bot.interactivity.approval.bad_payload")
        return
    action = _approval_decision(interaction, payload)
    request_key = payload.request_key
    clicker = interaction.clicker_id
    admin_env = _slack_envelope(
        interaction.channel_id,
        interaction.thread_ts or interaction.message_ts,
        deps,
        tag="approval",
    )

    if not _authorize_approval(interaction, deps):
        log.warning(
            "bot.interactivity.approval.unauthorized",
            clicker=clicker,
            request_key=request_key,
            channel=interaction.channel_id,
        )
        if admin_env is not None:
            _deliver_text(
                admin_env,
                f":no_entry: Only an admin can approve or deny requests. "
                f"(attempted by <@{clicker}>)",
                deps,
            )
        return

    approvals = deps.approvals if deps.approvals is not None else _load_approvals()
    if approvals is None:
        log.error("bot.interactivity.approval.no_module", request_key=request_key)
        if admin_env is not None:
            _deliver_text(
                admin_env,
                ":warning: Approvals aren't available right now — please try again shortly.",
                deps,
            )
        return

    # Capture the requester + prior status from the pending row BEFORE the decision, so we can tell a
    # first (state-changing) decision from an idempotent repeat and notify the requester exactly once.
    pending = _load_pending(deps.session_factory, request_key)
    prev_status = pending[2] if pending else ""

    ok = _apply_decision(approvals, action, request_key, clicker, deps)
    first_decision = ok and prev_status == "pending"
    log.info(
        "bot.interactivity.approval.decided",
        action=action,
        request_key=request_key,
        approver=clicker,
        ok=ok,
        first_decision=first_decision,
    )
    if admin_env is not None:
        if not ok:
            _deliver_text(
                admin_env,
                f"Couldn't {action} request `{request_key}` — it may already be decided.",
                deps,
            )
        elif first_decision:
            verb = "approved" if action == "approve" else "denied"
            _deliver_text(
                admin_env,
                f":white_check_mark: <@{clicker}> {verb} request `{request_key}`.",
                deps,
            )
        else:
            _deliver_text(
                admin_env,
                f"Request `{request_key}` was already handled — no change made.",
                deps,
            )
    if first_decision:
        _notify_requester(payload, action, pending, deps)


def _handle_request_approval(interaction: Interaction, deps: InteractivityDeps) -> None:
    """The requester's *Request approval* confirm click (PLAN §3.4): the ONLY step that pings the admin.

    FAIL-CLOSED authorization: the clicker MUST be the original requester (``advance_and_notify`` verifies
    ``clicker == row.requester`` and refuses otherwise) — so a bystander who sees the confirm card in a
    shared channel can never trigger the request on someone else's behalf. Transitions the row
    ``awaiting_confirmation → pending`` and notifies the admin ONCE (idempotent: a second click is a no-op
    that never re-pings). The requester is answered in the confirm card's own thread.
    """
    payload = interaction.payload
    if not isinstance(
        payload, RequestApprovalPayload
    ):  # dispatcher guarantees; defence in depth
        log.warning("bot.interactivity.request_approval.bad_payload")
        return
    request_key = payload.request_key
    clicker = interaction.clicker_id
    origin_env = _slack_envelope(
        interaction.channel_id,
        interaction.thread_ts or interaction.message_ts,
        deps,
        tag="request_approval",
    )

    approvals = deps.approvals if deps.approvals is not None else _load_approvals()
    if approvals is None or deps.session_factory is None:
        log.error(
            "bot.interactivity.request_approval.not_wired", request_key=request_key
        )
        if origin_env is not None:
            _deliver_text(
                origin_env,
                ":warning: Approvals aren't available right now — please try again shortly.",
                deps,
            )
        return

    notifier = approvals.AdminNotifier(
        service=deps.service, post_blocks=deps.post_blocks
    )
    result = "error"
    try:
        with deps.session_factory() as session:
            result = approvals.advance_and_notify(
                session,
                request_key,
                notifier=notifier,
                settings=deps.settings,
                requester_id=clicker,
            )
            session.commit()
    except Exception:  # noqa: BLE001 — fail-soft: the ack is already sent, never crash the route
        log.exception(
            "bot.interactivity.request_approval.failed", request_key=request_key
        )

    log.info(
        "bot.interactivity.request_approval.result",
        request_key=request_key,
        clicker=clicker,
        result=result,
    )
    if origin_env is None:
        return
    if result == "notified":
        _deliver_text(
            origin_env,
            "Thanks — I've asked the admins to approve. I'll run it right here once they do.",
            deps,
        )
    elif result == "already":
        _deliver_text(
            origin_env,
            "You've already requested this — it's waiting on an admin. I'll run it here once approved.",
            deps,
        )
    elif result == "forbidden":
        _deliver_text(
            origin_env,
            ":no_entry: Only the person who made this request can send it for approval.",
            deps,
        )
    else:  # missing / denied / error
        _deliver_text(
            origin_env,
            "Sorry — I couldn't find that request anymore. Please start again by sending me the document.",
            deps,
        )


def _approval_decision(interaction: Interaction, payload: ApprovalPayload) -> str:
    """The approve/deny decision: the producer's ``value.action`` is authoritative; the ``action_id`` is
    the fallback (either identifies the button)."""
    a = (payload.action or "").strip().lower()
    if a in ("approve", "deny"):
        return a
    _, deny_id = _approval_action_ids()
    return "deny" if interaction.action_id == deny_id else "approve"


def _clicker_is_admin_identity(clicker_id: str, deps: InteractivityDeps) -> bool:
    """True iff the Slack clicker resolves to an ACTIVE admin ``UserAccount`` OR an ``nda_allowlist`` row
    with ``role='admin'``. FAILS CLOSED — any lookup error returns False (never grants authorization)."""
    if not clicker_id:
        return False
    try:
        from app.db import SessionLocal

        from .approvals import resolve_account
        from .models import NdaAllowlist

        factory = deps.session_factory or SessionLocal
        with factory() as session:
            account = resolve_account(session, "slack", clicker_id)
            if account is not None and account.role == "admin":
                return True
            row = session.execute(
                select(NdaAllowlist.id)
                .where(
                    NdaAllowlist.principal_type == "slack",
                    NdaAllowlist.principal_key == clicker_id,
                    NdaAllowlist.role == "admin",
                )
                .limit(1)
            ).first()
            return row is not None
    except Exception as exc:  # noqa: BLE001 — fail CLOSED: a lookup error grants nothing
        log.error("bot.interactivity.approval.authz_lookup_failed", error=repr(exc))
        return False


def _authorize_approval(interaction: Interaction, deps: InteractivityDeps) -> bool:
    """Fail-closed admin authorization for an approve/deny click (PLAN §3.4, §6; reference §3.4).

    Authorized iff ANY of: the click ORIGINATED in the (dashboard-resolved) admin channel; the clicker
    resolves to an ACTIVE admin web account or an ``admin`` allowlist row; or an injected ``is_admin``
    predicate matches. With none matched, the click is denied. Fail CLOSED on any lookup error. The
    clicker id is always logged (the audit requirement).
    """
    try:
        from ..settings_store import admin_routing

        admin_channel, _ = admin_routing(settings_obj=deps.settings)
    except Exception:  # noqa: BLE001 — fall back to env if the store is unreachable
        admin_channel = (
            deps.settings.nda_admin_slack_channel if deps.settings else ""
        ) or ""
    from_admin_channel = bool(admin_channel) and interaction.channel_id == admin_channel
    is_admin_pred = (
        bool(deps.is_admin(interaction.clicker_id)) if deps.is_admin else False
    )
    is_admin_identity = _clicker_is_admin_identity(interaction.clicker_id, deps)
    authorized = from_admin_channel or is_admin_pred or is_admin_identity
    log.info(
        "bot.interactivity.approval.authz",
        clicker=interaction.clicker_id,
        channel=interaction.channel_id,
        admin_channel=admin_channel,
        from_admin_channel=from_admin_channel,
        is_admin=is_admin_pred,
        is_admin_identity=is_admin_identity,
        authorized=authorized,
    )
    return authorized


def _apply_decision(
    approvals: Any,
    action: str,
    request_key: str,
    clicker: str,
    deps: InteractivityDeps,
) -> bool:
    """Call ``approve_request`` / ``deny_request`` (the approvals contract) in a session; return its bool.

    Commits after the call so a caller-managed-transaction implementation persists; an already-committing
    implementation just no-ops the extra commit. Any failure degrades to ``False`` (fail-soft)."""
    fn = getattr(
        approvals,
        "approve_request" if action == "approve" else "deny_request",
        None,
    )
    if not callable(fn):
        log.error("bot.interactivity.approval.missing_fn", action=action)
        return False
    if deps.session_factory is None:
        log.error("bot.interactivity.approval.no_session", request_key=request_key)
        return False
    try:
        with deps.session_factory() as session:
            if action == "approve":
                # Pass the delivery seam so approve_request can auto-run the stashed review and deliver
                # it to the ORIGIN (PLAN §3.4 D). ``deny_request`` takes no delivery deps.
                ok = bool(
                    fn(
                        session,
                        request_key,
                        clicker,
                        service=deps.service,
                        post_blocks=deps.post_blocks,
                        settings=deps.settings,
                    )
                )
            else:
                ok = bool(fn(session, request_key, clicker))
            session.commit()
            return ok
    except Exception:  # noqa: BLE001 - a decision failure is reported, never crashes the ack path
        log.exception(
            "bot.interactivity.approval.apply_failed", request_key=request_key
        )
        return False


def _notify_requester(
    payload: ApprovalPayload,
    action: str,
    pending: tuple[str, str, str] | None,
    deps: InteractivityDeps,
) -> None:
    """Notify the ORIGINAL requester of the decision (the ported DM-to-requester behavior, reference §3.11).

    Requester delivery context is resolved in priority order: the typed payload (a future producer's
    inline bridge), a ``bot_correlation`` row keyed by the request key (a richer future store), then the
    persisted ``NdaPendingRequest`` row (``pending`` — its stored ``requester`` + ``channel``: DM the
    Slack user id / email the address). Absent all three, it logs and skips (the decision still stands)."""
    ctx = _requester_context(payload, pending, deps)
    if ctx is None:
        log.info(
            "bot.interactivity.approval.no_requester_ctx",
            request_key=payload.request_key,
        )
        return
    text = _requester_text(action, payload.intent)
    from_email = deps.settings.nda_bot_from_email if deps.settings else ""
    if ctx["kind"] == "email":
        envelope = Envelope(
            channel="email",
            event_key=f"slack:int:notify:{payload.request_key}",
            sender_address=str(ctx["channel"]),
            email_message_id=str(ctx.get("thread") or ""),
            email_subject=str(ctx.get("subject") or "your NDA request"),
            from_email=from_email,
        )
    else:
        # Slack: ``channel`` is the requester's user id (a DM) or a channel id — post un-threaded.
        envelope = Envelope(
            channel="slack",
            event_key=f"slack:int:notify:{payload.request_key}",
            slack_channel=str(ctx["channel"]),
            slack_thread_ts=str(ctx.get("thread") or ""),
            verified_sender=True,
            from_email=from_email,
        )
    _deliver_text(envelope, text, deps)


def _requester_context(
    payload: ApprovalPayload,
    pending: tuple[str, str, str] | None,
    deps: InteractivityDeps,
) -> dict[str, Any] | None:
    """Resolve the requester's delivery context: typed payload → bot_correlation → the pending row."""
    if payload.requester_channel:
        return {
            "kind": payload.requester_channel_kind or "slack",
            "channel": payload.requester_channel,
            "thread": payload.requester_thread_ts,
            "id": payload.requester_id,
            "subject": payload.requester_subject,
        }
    if deps.session_factory is not None:
        stored = _load_correlation(
            deps.session_factory,
            _approval_correlation_key(payload.request_key),
            KIND_APPROVAL,
        )
        if stored and (stored.get("channel") or stored.get("slack_channel")):
            return {
                "kind": stored.get("kind") or "slack",
                "channel": stored.get("channel") or stored.get("slack_channel"),
                "thread": stored.get("thread") or stored.get("slack_thread_ts") or "",
                "id": stored.get("id") or stored.get("requester") or "",
                "subject": stored.get("subject") or "",
            }
    if pending is not None:
        requester, channel, _status = pending
        if requester:
            # Slack requester => DM their user id; email requester => mail the stored address.
            return {
                "kind": channel or "slack",
                "channel": requester,
                "thread": "",
                "id": requester,
                "subject": "",
            }
    return None


def _load_pending(
    session_factory: SessionFactory | None, request_key: str
) -> tuple[str, str, str] | None:
    """Read ``(requester, channel, status)`` off the ``NdaPendingRequest`` row by ``request_key``.

    ``None`` when there is no such row or on any read failure. ``requester`` is the Slack user id or the
    email address the approvals gate persisted; ``channel`` is ``slack`` / ``email``; ``status`` lets the
    handler distinguish a first (state-changing) decision from an idempotent repeat."""
    if session_factory is None:
        return None
    try:
        with session_factory() as session:
            row = session.execute(
                select(NdaPendingRequest).where(
                    NdaPendingRequest.request_key == request_key
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return (row.requester or "", row.channel or "", row.status or "")
    except Exception as exc:  # noqa: BLE001 - a lookup failure must never crash the ack path
        log.warning(
            "bot.interactivity.pending_read_failed",
            request_key=request_key,
            error=repr(exc),
        )
        return None


def _requester_text(action: str, intent: str) -> str:
    label = intent or "request"
    if action == "approve":
        return (
            f"Good news — an admin approved your *{label}*. Please resend it and "
            f"I'll run it now."
        )
    return (
        f"An admin reviewed your *{label}* and didn't approve it this time. "
        f"Reach out to the team if you have questions."
    )


# =========================================================================== #
# Small helpers (state reading, correlation, delivery, envelope building)
# =========================================================================== #
def _dig(data: Any, *keys: str) -> Any:
    """Safely read a nested key path off a (possibly missing) dict; ``None`` if any hop is absent."""
    cur = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _selected(state_values: dict[str, Any], action_id: str) -> str:
    """Read a static-select's chosen VALUE out of Slack ``state.values`` by ``action_id`` (block-id agnostic).

    Slack keys ``state.values`` by an auto-generated block id then the action id; the wave-A picker sets
    action ids but not block ids, so we scan blocks for the action (reference §3.7 derives the selects
    this way). Missing / unselected → ``''`` (the ported "empty means not chosen" default)."""
    for block in (state_values or {}).values():
        if isinstance(block, dict) and action_id in block:
            element = block.get(action_id) or {}
            option = (
                element.get("selected_option") if isinstance(element, dict) else None
            )
            if isinstance(option, dict):
                return str(option.get("value", "") or "")
    return ""


def _load_correlation(
    session_factory: SessionFactory, key: str, kind: str
) -> dict[str, Any] | None:
    """Read a ``bot_correlation`` payload by (key, kind); ``None`` when absent or on any read failure."""
    try:
        with session_factory() as session:
            row = session.execute(
                select(BotCorrelation)
                .where(BotCorrelation.key == key, BotCorrelation.kind == kind)
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            return dict(row.payload_json or {})
    except Exception as exc:  # noqa: BLE001 - a correlation miss must never crash the turn
        log.warning(
            "bot.interactivity.correlation_read_failed", key=key, error=repr(exc)
        )
        return None


def _slack_envelope(
    channel_id: str, thread_ts: str, deps: InteractivityDeps, *, tag: str
) -> Envelope | None:
    """Build a minimal Slack :class:`Envelope` addressed at (channel, thread) for a reply. ``None`` when
    no channel is resolvable (nothing to reply to — logged, never a crash)."""
    if not channel_id:
        log.info("bot.interactivity.no_channel", tag=tag)
        return None
    from_email = deps.settings.nda_bot_from_email if deps.settings else ""
    return Envelope(
        channel="slack",
        event_key=f"slack:int:{tag}:{channel_id}:{thread_ts or 'root'}",
        slack_channel=channel_id,
        slack_thread_ts=thread_ts or "",
        verified_sender=True,
        from_email=from_email,
    )


def _deliver_text(envelope: Envelope, text: str, deps: InteractivityDeps) -> None:
    """Deliver a plain (mrkdwn) text reply through the channel-aware service (no-op if none wired)."""
    if deps.service is None:
        log.info("bot.interactivity.deliver_skipped", event_key=envelope.event_key)
        return
    try:
        deps.service.deliver(envelope, Reply(text=text))
    except Exception as exc:  # noqa: BLE001 - delivery is fail-soft
        log.warning(
            "bot.interactivity.deliver_failed",
            event_key=envelope.event_key,
            error=repr(exc),
        )


def _deliver_intent_reply(
    envelope: Envelope, reply: Any, deps: InteractivityDeps
) -> None:
    """Deliver an intent handler's reply: a file (if the reply carries attachments), else a Block Kit
    card (Slack), else text — mirroring the router's own delivery fork (reference §3.8/§3.9).

    ``attachments`` is read defensively so this works both with the current text/blocks ``IntentReply``
    and a future file-bearing one (the template intent's ``.docx`` reply)."""
    if deps.service is None:
        log.info("bot.interactivity.deliver_skipped", event_key=envelope.event_key)
        return
    text = getattr(reply, "text", "") or ""
    blocks = getattr(reply, "slack_blocks", None)
    attachments = getattr(reply, "attachments", None)
    try:
        if attachments:
            deps.service.deliver(
                envelope, Reply(text=text, attachments=tuple(attachments))
            )
        elif blocks and envelope.channel == "slack" and deps.post_blocks is not None:
            fallback = getattr(reply, "fallback_text", "") or text
            deps.post_blocks(envelope, list(blocks), fallback)
        else:
            deps.service.deliver(envelope, Reply(text=text))
    except Exception as exc:  # noqa: BLE001 - delivery is fail-soft
        log.warning(
            "bot.interactivity.deliver_failed",
            event_key=envelope.event_key,
            error=repr(exc),
        )


def _reply_expired(interaction: Interaction, deps: InteractivityDeps) -> None:
    """Post the friendly "this button expired" reply into the interaction's own thread (PLAN §3.3)."""
    envelope = _slack_envelope(
        interaction.channel_id,
        interaction.thread_ts or interaction.message_ts,
        deps,
        tag="expired",
    )
    if envelope is None:
        return
    _deliver_text(envelope, EXPIRED_TEXT, deps)


__all__ = [
    "PAYLOAD_VERSION",
    "KIND_TEMPLATE_PICKER",
    "KIND_APPROVAL",
    "KIND_REQUEST_APPROVAL",
    "KIND_IGNORE",
    "InteractivityError",
    "ButtonPayload",
    "ApprovalPayload",
    "RequestApprovalPayload",
    "Interaction",
    "InteractionHandler",
    "InteractivityRegistry",
    "InteractivityDeps",
    "default_interactivity_registry",
    "dispatch_interaction",
]
