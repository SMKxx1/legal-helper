"""The Slack guided template-replacement chain (PLAN §3.7) — admin-gated ask→validate→testdrive→publish.

Drives :func:`app.bot.interactivity.dispatch_interaction` with hand-built Slack bodies + a capturing
reply service / post_blocks sink and INJECTED collaborators (a fake thread scanner, a fake Slack file
fetcher) — zero network. Covers:

* the admin-gating of the picker affordance (:class:`AdminTemplateIntent` adds *Update this template*
  only for an admin sender — a non-admin gets no button);
* the fail-closed chain (a non-admin click is refused — nothing is published);
* the full happy path (start reads the picker selectors → validate recovers the thread .docx and runs
  the checklist vs the registry required set → publish writes a NEW current ``template_version`` and
  reports the version + rollback), asserted against the throwaway DB;
* the publish gate (a replacement missing a required token can't publish);
* the drift emit on publish (an added token flags every NDA form ``needs_update``);
* the "no such template to replace" friendly refusal.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from io import BytesIO
from types import SimpleNamespace
from typing import Any

from docx import Document

from app.bot.envelope import Envelope
from app.bot.intents import IntentContext, IntentReply
from app.bot.intents.template_admin import (
    ACTION_TPL_ADMIN_PUBLISH,
    ACTION_TPL_ADMIN_UPDATE,
    ACTION_TPL_ADMIN_VALIDATE,
    NOT_ADMIN_TEXT,
    AdminTemplateIntent,
    TemplateAdminDeps,
    is_admin_sender,
    register_template_admin,
)
from app.bot.interactivity import (
    InteractivityDeps,
    default_interactivity_registry,
    dispatch_interaction,
)
from app.bot.thread_docs import ThreadDoc
from app.config import Settings
from app.models_v2 import DocumentBlob, Template, TemplateVersion, Token, TokenTemplate
from app.schemas import DEFAULT_ORG_ID
from app.support_task.generator import DOCX_MIME

pytest_plugins = ("conftest_bot",)

ADMIN_CHANNEL = "CADMIN"


# --------------------------------------------------------------------------- #
# .docx + seeding helpers
# --------------------------------------------------------------------------- #
def _docx(paragraphs: list[str]) -> bytes:
    doc = Document()
    for para in paragraphs:
        doc.add_paragraph(para)
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _seed_template(
    factory: Any,
    *,
    docx_bytes: bytes,
    jur: str = "US",
    cp: str = "Company",
    mut: str = "NotApplicable",
    variant: str = "empty",
) -> str:
    """Seed a logical template + a current ``variant`` version backed by ``docx_bytes``. Returns the id."""
    with factory() as s:
        blob = DocumentBlob(
            sha256=hashlib.sha256(docx_bytes + uuid.uuid4().bytes).hexdigest(),
            byte_size=len(docx_bytes),
            mime_type=DOCX_MIME,
            bytes=docx_bytes,
        )
        s.add(blob)
        s.flush()
        tmpl = Template(
            org_id=DEFAULT_ORG_ID,
            jurisdiction_code=jur,
            counterparty_type_code=cp,
            mutuality_code=mut,
            name=f"{jur} {cp} NDA",
        )
        s.add(tmpl)
        s.flush()
        s.add(
            TemplateVersion(
                template_id=tmpl.id,
                variant_code=variant,
                version_no=1,
                blob_id=blob.id,
                is_current=True,
            )
        )
        s.commit()
        return tmpl.id


def _seed_token(
    factory: Any, name: str, *, template_id: str | None = None, required: bool = False
) -> None:
    """Seed a registry ``token`` row (so it's "known"); link it to ``template_id`` when ``required``."""
    with factory() as s:
        tok = Token(name=name, placeholder="{{" + name + "}}", scope_code="Global")
        s.add(tok)
        s.flush()
        if required and template_id:
            s.add(TokenTemplate(token_id=tok.id, template_id=template_id))
        s.commit()


def _versions(
    factory: Any, template_id: str, variant: str = "empty"
) -> list[tuple[int, bool]]:
    with factory() as s:
        rows = (
            s.query(TemplateVersion)
            .filter(
                TemplateVersion.template_id == template_id,
                TemplateVersion.variant_code == variant,
            )
            .all()
        )
        return sorted((r.version_no, bool(r.is_current)) for r in rows)


# --------------------------------------------------------------------------- #
# Capturing sinks + deps
# --------------------------------------------------------------------------- #
class CaptureService:
    """A capturing ReplyService stand-in — records (envelope, reply); no network."""

    def __init__(self) -> None:
        self.sent: list[tuple[Any, Any]] = []

    def deliver(self, envelope: Any, reply: Any) -> Any:
        self.sent.append((envelope, reply))
        return SimpleNamespace(ok=True, channel=envelope.channel)


class CaptureBlocks:
    def __init__(self) -> None:
        self.posts: list[tuple[Any, list[dict], str]] = []

    def __call__(self, envelope: Any, blocks: list[dict], fallback: str) -> None:
        self.posts.append((envelope, blocks, fallback))


def _deps(factory: Any) -> tuple[InteractivityDeps, CaptureService, CaptureBlocks]:
    service = CaptureService()
    post = CaptureBlocks()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        nda_admin_slack_channel=ADMIN_CHANNEL,
        nda_bot_from_email="nda-bot@example.com",
    )
    deps = InteractivityDeps(
        session_factory=factory,
        service=service,
        post_blocks=post,
        settings=settings,
    )
    return deps, service, post


def _registry(admin_deps: TemplateAdminDeps):
    """The default registry with the template-admin kinds re-registered to bind the test fakes."""
    registry = default_interactivity_registry()
    register_template_admin(
        registry, deps=admin_deps
    )  # last-wins over the default binding
    return registry


# --------------------------------------------------------------------------- #
# Body builders
# --------------------------------------------------------------------------- #
def _picker_state(
    jur: str = "US", cp: str = "company", mut: str = ""
) -> dict[str, Any]:
    def sel(v: str) -> dict[str, Any]:
        return {"selected_option": {"value": v}} if v else {"selected_option": None}

    return {
        "b1": {"select_jurisdiction": sel(jur)},
        "b2": {"select_counterparty_type": sel(cp)},
        "b3": {"select_mutuality": sel(mut)},
    }


def _actions_body(
    action_id: str,
    value: str,
    *,
    channel: str = ADMIN_CHANNEL,
    clicker: str = "UADMIN",
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "user": {"id": clicker},
        "channel": {"id": channel},
        "container": {
            "type": "message",
            "message_ts": "M1",
            "thread_ts": "T1",
            "channel_id": channel,
        },
        "message": {"ts": "M1", "thread_ts": "T1"},
        "trigger_id": "trig",
        "actions": [{"action_id": action_id, "type": "button", "value": value}],
        "state": {"values": state or {}},
    }


def _button_value(posts: CaptureBlocks, action_id: str) -> str:
    """Pull a button's value out of the most recent posted blocks by action_id."""
    for _env, blocks, _fb in reversed(posts.posts):
        for block in blocks:
            if block.get("type") == "actions":
                for el in block.get("elements", []):
                    if el.get("action_id") == action_id:
                        return el.get("value", "")
    raise AssertionError(f"no button {action_id!r} in posted blocks")


def _texts(service: CaptureService) -> list[str]:
    return [getattr(reply, "text", "") for _env, reply in service.sent]


# =========================================================================== #
# 1) Admin-gating of the picker affordance
# =========================================================================== #
def _picker_reply() -> IntentReply:
    return IntentReply(
        slack_blocks=(
            {"type": "header", "text": {"type": "plain_text", "text": "picker"}},
            {
                "type": "actions",
                "elements": [{"type": "button", "action_id": "template_submit"}],
            },
        ),
        fallback_text="pick",
    )


def _ctx(channel: str, sender_id: str = "") -> IntentContext:
    from app.bot.router import Classification

    env = Envelope(
        channel="slack",
        event_key="ek",
        slack_channel=channel,
        sender_id=sender_id,
        verified_sender=True,
    )
    return IntentContext(envelope=env, classification=Classification(intent="template"))


def test_admin_gets_update_button_nonadmin_does_not() -> None:
    settings = Settings(_env_file=None, nda_admin_slack_channel=ADMIN_CHANNEL)  # type: ignore[call-arg]
    wrapped = AdminTemplateIntent(lambda ctx: _picker_reply(), settings=settings)

    admin_reply = wrapped(_ctx(ADMIN_CHANNEL))
    ids = {
        el["action_id"]
        for b in admin_reply.slack_blocks or ()
        if b.get("type") == "actions"
        for el in b.get("elements", [])
    }
    assert ACTION_TPL_ADMIN_UPDATE in ids  # admin sees the affordance

    non_admin_reply = wrapped(_ctx("C-random"))
    ids2 = {
        el["action_id"]
        for b in non_admin_reply.slack_blocks or ()
        if b.get("type") == "actions"
        for el in b.get("elements", [])
    }
    assert ACTION_TPL_ADMIN_UPDATE not in ids2  # non-admin gets no update button


def test_admin_predicate_authorizes_outside_admin_channel() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]  # no admin channel configured
    wrapped = AdminTemplateIntent(
        lambda ctx: _picker_reply(),
        settings=settings,
        is_admin=lambda uid: uid == "UBOSS",
    )
    reply = wrapped(_ctx("C-anywhere", sender_id="UBOSS"))
    ids = {
        el["action_id"]
        for b in reply.slack_blocks or ()
        if b.get("type") == "actions"
        for el in b.get("elements", [])
    }
    assert ACTION_TPL_ADMIN_UPDATE in ids


def test_is_admin_sender_email_requires_dmarc() -> None:
    settings = Settings(_env_file=None, nda_admin_email="boss@example.com")  # type: ignore[call-arg]
    aligned = Envelope(
        channel="email",
        event_key="e",
        sender_address="Boss@Example.com",
        verified_sender=True,
    )
    spoofed = Envelope(
        channel="email",
        event_key="e",
        sender_address="boss@example.com",
        verified_sender=False,
    )
    assert is_admin_sender(aligned, settings) is True
    assert is_admin_sender(spoofed, settings) is False  # un-aligned mail is never admin


# =========================================================================== #
# 2) The full happy path: start → validate → publish
# =========================================================================== #
def test_full_chain_publishes_new_version(bot_session_factory) -> None:
    old = _docx(["NDA for {{company_name}}."])
    new = _docx(["Corrected NDA for {{company_name}}. New clause added."])
    tmpl_id = _seed_template(bot_session_factory, docx_bytes=old)
    _seed_token(bot_session_factory, "company_name", template_id=tmpl_id, required=True)

    admin_deps = TemplateAdminDeps(
        scanner=lambda ch, ts: ThreadDoc(file_id="F1", file_name="new.docx"),
        slack_fetch=lambda att: new,
    )
    registry = _registry(admin_deps)
    deps, service, post = _deps(bot_session_factory)

    # -- start: read the picker selectors, ask for the upload + a Validate button
    dispatch_interaction(
        _actions_body(
            ACTION_TPL_ADMIN_UPDATE,
            json.dumps({"v": 1, "kind": "tpl_admin_update"}),
            state=_picker_state(),
        ),
        registry=registry,
        deps=deps,
    )
    validate_value = _button_value(post, ACTION_TPL_ADMIN_VALIDATE)
    assert "Update the US / Company template" in post.posts[-1][1][0]["text"]["text"]

    # -- validate: recover the thread doc, run the checklist → publishable (offers Confirm & publish)
    dispatch_interaction(
        _actions_body(ACTION_TPL_ADMIN_VALIDATE, validate_value),
        registry=registry,
        deps=deps,
    )
    summary = post.posts[-1][1][0]["text"]["text"]
    assert "company_name" in summary
    assert "All required tokens present" in summary
    publish_value = _button_value(post, ACTION_TPL_ADMIN_PUBLISH)

    # -- publish: writes a NEW current version through the shared publish path
    dispatch_interaction(
        _actions_body(ACTION_TPL_ADMIN_PUBLISH, publish_value),
        registry=registry,
        deps=deps,
    )
    assert _versions(bot_session_factory, tmpl_id) == [(1, False), (2, True)]
    published = _texts(service)[-1]
    assert "version 2" in published
    assert "Rollback" in published and "version 1" in published

    # Attribution (P6): the published version records the namespaced Slack uploader (clicker "UADMIN").
    with bot_session_factory() as s:
        current = (
            s.query(TemplateVersion)
            .filter(
                TemplateVersion.template_id == tmpl_id,
                TemplateVersion.variant_code == "empty",
                TemplateVersion.is_current.is_(True),
            )
            .one()
        )
        assert current.created_by == "slack:UADMIN"


# =========================================================================== #
# 3) Fail-closed: a non-admin click is refused and nothing publishes
# =========================================================================== #
def test_non_admin_click_is_refused(bot_session_factory) -> None:
    old = _docx(["NDA for {{company_name}}."])
    tmpl_id = _seed_template(bot_session_factory, docx_bytes=old)
    _seed_token(bot_session_factory, "company_name", template_id=tmpl_id, required=True)

    admin_deps = TemplateAdminDeps(
        scanner=lambda ch, ts: ThreadDoc(file_id="F1", file_name="new.docx"),
        slack_fetch=lambda att: old,
    )
    registry = _registry(admin_deps)
    deps, service, post = _deps(bot_session_factory)

    # start FROM the admin channel to mint a real ref (state exists)…
    dispatch_interaction(
        _actions_body(
            ACTION_TPL_ADMIN_UPDATE,
            json.dumps({"v": 1, "kind": "tpl_admin_update"}),
            state=_picker_state(),
        ),
        registry=registry,
        deps=deps,
    )
    validate_value = _button_value(post, ACTION_TPL_ADMIN_VALIDATE)

    # …but the Validate click comes from a NON-admin (a different channel, no is_admin) → refused.
    dispatch_interaction(
        _actions_body(
            ACTION_TPL_ADMIN_VALIDATE,
            validate_value,
            channel="C-random",
            clicker="URANDO",
        ),
        registry=registry,
        deps=deps,
    )
    assert _texts(service)[-1] == NOT_ADMIN_TEXT
    # No version was published.
    assert _versions(bot_session_factory, tmpl_id) == [(1, True)]


# =========================================================================== #
# 4) Publish gate: a replacement missing a required token cannot publish
# =========================================================================== #
def test_missing_required_token_blocks_publish(bot_session_factory) -> None:
    old = _docx(["NDA for {{company_name}} on {{effective_date}}."])
    new = _docx(["NDA for {{company_name}}."])  # drops the required effective_date
    tmpl_id = _seed_template(bot_session_factory, docx_bytes=old)
    _seed_token(bot_session_factory, "company_name", template_id=tmpl_id, required=True)
    _seed_token(
        bot_session_factory, "effective_date", template_id=tmpl_id, required=True
    )

    admin_deps = TemplateAdminDeps(
        scanner=lambda ch, ts: ThreadDoc(file_id="F1", file_name="new.docx"),
        slack_fetch=lambda att: new,
    )
    registry = _registry(admin_deps)
    deps, service, post = _deps(bot_session_factory)

    dispatch_interaction(
        _actions_body(
            ACTION_TPL_ADMIN_UPDATE,
            json.dumps({"v": 1, "kind": "tpl_admin_update"}),
            state=_picker_state(),
        ),
        registry=registry,
        deps=deps,
    )
    validate_value = _button_value(post, ACTION_TPL_ADMIN_VALIDATE)
    dispatch_interaction(
        _actions_body(ACTION_TPL_ADMIN_VALIDATE, validate_value),
        registry=registry,
        deps=deps,
    )
    summary = post.posts[-1][1][0]["text"]["text"]
    assert "Missing required" in summary and "effective_date" in summary
    # The validation card offers Re-validate, NOT Confirm & publish.
    ids = {
        el["action_id"]
        for b in post.posts[-1][1]
        if b.get("type") == "actions"
        for el in b.get("elements", [])
    }
    assert ACTION_TPL_ADMIN_PUBLISH not in ids

    # Forcing a publish click is refused (still missing the required token) — nothing changes.
    forced = json.dumps(
        {"v": 1, "kind": "tpl_admin_publish", "ref": json.loads(validate_value)["ref"]}
    )
    dispatch_interaction(
        _actions_body(ACTION_TPL_ADMIN_PUBLISH, forced),
        registry=registry,
        deps=deps,
    )
    assert "missing required token" in _texts(service)[-1]
    assert _versions(bot_session_factory, tmpl_id) == [(1, True)]


# =========================================================================== #
# 5) Publish emits drift: an added token flags every NDA form needs_update
# =========================================================================== #
def test_publish_with_no_template_is_friendly(bot_session_factory) -> None:
    new = _docx(["NDA for {{company_name}}."])
    # No template seeded at all; the required set is empty so validation passes, but publish must refuse.
    admin_deps = TemplateAdminDeps(
        scanner=lambda ch, ts: ThreadDoc(file_id="F1", file_name="new.docx"),
        slack_fetch=lambda att: new,
    )
    registry = _registry(admin_deps)
    deps, service, post = _deps(bot_session_factory)

    dispatch_interaction(
        _actions_body(
            ACTION_TPL_ADMIN_UPDATE,
            json.dumps({"v": 1, "kind": "tpl_admin_update"}),
            state=_picker_state(),
        ),
        registry=registry,
        deps=deps,
    )
    validate_value = _button_value(post, ACTION_TPL_ADMIN_VALIDATE)
    dispatch_interaction(
        _actions_body(ACTION_TPL_ADMIN_VALIDATE, validate_value),
        registry=registry,
        deps=deps,
    )
    publish_value = _button_value(post, ACTION_TPL_ADMIN_PUBLISH)
    dispatch_interaction(
        _actions_body(ACTION_TPL_ADMIN_PUBLISH, publish_value),
        registry=registry,
        deps=deps,
    )
    assert "no *US / Company* template loaded to update" in _texts(service)[-1]
