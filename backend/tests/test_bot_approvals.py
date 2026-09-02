"""The real allowlist / approvals gate (PLAN §3.4, reference §3.5) — fail-closed, keyed on VERIFIED id.

Exercises :mod:`app.bot.approvals` against a throwaway per-test SQLite DB (no network): the allow / deny /
pending matrix, the security-critical "unverified email is refused even when allowlisted" rule, the
duplicate-safe (no-dup) re-request, the idempotent approve (which ALSO adds the allowlist row) + deny, the
admin-notify Block Kit / email-fallback payload shapes (captured sinks), and — the load-bearing invariant —
that a DB/session error FAILS CLOSED (pending, NEVER allowed).
"""

from __future__ import annotations

import json
from typing import Any

from app.bot.approvals import (
    ACTION_APPROVAL_APPROVE,
    ACTION_APPROVAL_DENY,
    AdminNotifier,
    AllowlistGate,
    advance_and_notify,
    approve_request,
    deny_request,
    gate_check,
)
from app.bot.envelope import Envelope
from app.bot.models import NdaAllowlist, NdaPendingRequest
from app.bot.router import Classification, GateDecision
from app.config import Settings

# Shared bot-test fixtures (bot_session_factory) live in tests/conftest_bot.py (conftest.py is frozen).
pytest_plugins = ("conftest_bot",)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _slack_env(sender_id: str = "U9", *, text: str = "review this") -> Envelope:
    # Slack events are signature-verified upstream: sender_id ALWAYS counts as a verified identity.
    return Envelope(
        channel="slack",
        event_key=f"slack:{sender_id}:{text}",
        text=text,
        slack_channel="C1",
        slack_thread_ts="1.0",
        sender_id=sender_id,
        verified_sender=True,
    )


def _email_env(
    addr: str = "jane@x.com", *, verified: bool, text: str = "review this"
) -> Envelope:
    return Envelope(
        channel="email",
        event_key=f"email:<{addr}|{verified}>",
        text=text,
        sender_address=addr,
        email_message_id=f"<{addr}>",
        verified_sender=verified,
    )


def _settings(
    *, nda_admin_slack_channel: str = "", nda_admin_email: str = ""
) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        nda_admin_slack_channel=nda_admin_slack_channel,
        nda_admin_email=nda_admin_email,
    )


def _seed_allowlist(factory, principal_type: str, principal_key: str) -> None:
    with factory() as s, s.begin():
        s.add(
            NdaAllowlist(
                principal_type=principal_type,
                principal_key=principal_key,
                added_by="seed",
            )
        )


def _pending_rows(factory) -> list[NdaPendingRequest]:
    with factory() as s:
        return list(s.query(NdaPendingRequest).all())


def _allowlist_rows(factory) -> list[NdaAllowlist]:
    with factory() as s:
        return list(s.query(NdaAllowlist).all())


class _CapturePostBlocks:
    """A stand-in for the Slack sink's ``post_blocks`` — records every admin card posted."""

    def __init__(self) -> None:
        self.calls: list[tuple[Envelope, list, str]] = []

    def __call__(self, env: Envelope, blocks: list, fallback: str) -> str:
        self.calls.append((env, blocks, fallback))
        return "posted"


class _CaptureService:
    """A stand-in ``ReplyService`` — records the email fallback delivery."""

    def __init__(self) -> None:
        self.calls: list[tuple[Envelope, Any]] = []

    def deliver(self, env: Envelope, reply: Any) -> str:
        self.calls.append((env, reply))
        return "ok"


# --------------------------------------------------------------------------- #
# ALLOWED
# --------------------------------------------------------------------------- #
def test_allowed_when_slack_principal_on_allowlist(bot_session_factory) -> None:
    _seed_allowlist(bot_session_factory, "slack", "U9")
    with bot_session_factory() as s:
        d = gate_check(s, _slack_env("U9"), "review", settings=_settings())
    assert d == GateDecision(status="allowed")
    assert _pending_rows(bot_session_factory) == []  # allowed path persists nothing


def test_allowed_when_verified_email_on_allowlist(bot_session_factory) -> None:
    _seed_allowlist(bot_session_factory, "email", "jane@x.com")
    with bot_session_factory() as s:
        d = gate_check(
            s, _email_env("jane@x.com", verified=True), "envelope", settings=_settings()
        )
    assert d.status == "allowed"


def test_email_principal_is_lowercased_for_membership(bot_session_factory) -> None:
    _seed_allowlist(bot_session_factory, "email", "jane@x.com")
    with bot_session_factory() as s:
        d = gate_check(
            s, _email_env("JANE@X.com", verified=True), "review", settings=_settings()
        )
    assert (
        d.status == "allowed"
    )  # verified id is normalized to lower-case before the lookup


def _seed_user(
    factory, *, role="admin", email=None, slack_user_id=None, status="active"
) -> None:
    """Seed a web ``UserAccount`` (+ its org) so exemption can resolve the bot identity to a role."""
    import uuid

    from app.auth.models import Org, UserAccount
    from app.schemas import DEFAULT_ORG_ID

    with factory() as s:
        if s.get(Org, DEFAULT_ORG_ID) is None:
            s.add(Org(id=DEFAULT_ORG_ID, name="Default"))
            s.flush()
        s.add(
            UserAccount(
                org_id=DEFAULT_ORG_ID,
                user_id=f"u-{uuid.uuid4().hex[:8]}",
                email=email,
                slack_user_id=slack_user_id,
                role=role,
                status=status,
            )
        )
        s.commit()


def test_bare_admin_email_env_is_no_longer_a_bypass(bot_session_factory) -> None:
    # The old NDA_ADMIN_EMAIL identity bypass is REMOVED — exemption is now role/allowlist only.
    st = _settings(nda_admin_email="admin@x.com")
    with bot_session_factory() as s:
        d = gate_check(
            s, _email_env("Admin@X.com", verified=True), "envelope", settings=st
        )
    assert d.status == "needs_confirmation"  # not exempt -> must request approval


def test_admin_web_account_is_exempt_by_email(bot_session_factory) -> None:
    _seed_user(bot_session_factory, role="admin", email="admin@x.com")
    with bot_session_factory() as s:
        d = gate_check(
            s,
            _email_env("Admin@X.com", verified=True),
            "envelope",
            settings=_settings(),
        )
    assert d.status == "allowed"  # active admin account, matched case-insensitively
    assert (
        _allowlist_rows(bot_session_factory) == []
    )  # role exemption needs no allowlist row


def test_admin_web_account_is_exempt_by_slack_id(bot_session_factory) -> None:
    _seed_user(bot_session_factory, role="admin", slack_user_id="UADMIN")
    with bot_session_factory() as s:
        d = gate_check(s, _slack_env("UADMIN"), "review", settings=_settings())
    assert d.status == "allowed"


def test_non_admin_account_is_not_exempt(bot_session_factory) -> None:
    _seed_user(bot_session_factory, role="reviewer", slack_user_id="UREV")
    with bot_session_factory() as s:
        d = gate_check(s, _slack_env("UREV"), "review", settings=_settings())
    # only role=="admin" (or an allowlist row) is exempt; everyone else must request approval
    assert d.status == "needs_confirmation"


def test_disabled_admin_account_is_not_exempt(bot_session_factory) -> None:
    _seed_user(
        bot_session_factory, role="admin", slack_user_id="UOFF", status="disabled"
    )
    with bot_session_factory() as s:
        d = gate_check(s, _slack_env("UOFF"), "review", settings=_settings())
    assert d.status == "needs_confirmation"  # only ACTIVE admin accounts count


# --------------------------------------------------------------------------- #
# REFUSED / PENDING
# --------------------------------------------------------------------------- #
def test_unverified_email_refused_even_when_allowlisted(bot_session_factory) -> None:
    # THE security invariant (§3.3/§6): an un-aligned email can never match the allowlist.
    _seed_allowlist(bot_session_factory, "email", "jane@x.com")
    with bot_session_factory() as s:
        d = gate_check(
            s, _email_env("jane@x.com", verified=False), "review", settings=_settings()
        )
    # NOT allowed, despite the address being on the allowlist — an unverified sender must request.
    assert d.status == "needs_confirmation"
    rows = _pending_rows(bot_session_factory)
    assert (
        len(rows) == 1
        and rows[0].requester == "jane@x.com"
        and rows[0].channel == "email"
        and rows[0].status == "awaiting_confirmation"
    )


def test_admin_slack_channel_is_not_an_identity(bot_session_factory) -> None:
    # NDA_ADMIN_SLACK_CHANNEL is a channel, not an identity — a Slack user whose id equals it is NOT
    # auto-allowed. No env admin bypass exists for the Slack plane (admins are allowlist rows).
    st = _settings(nda_admin_slack_channel="U_ADMIN", nda_admin_email="admin@x.com")
    with bot_session_factory() as s:
        d = gate_check(s, _slack_env("U_ADMIN"), "review", settings=st)
    assert d.status == "needs_confirmation"


def test_miss_creates_awaiting_confirmation_and_returns_request_key(
    bot_session_factory,
) -> None:
    with bot_session_factory() as s:
        d = gate_check(s, _slack_env("U9"), "review", settings=_settings())
    assert d.status == "needs_confirmation"
    assert d.request_key.startswith("req_")
    rows = _pending_rows(bot_session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert row.requester == "U9"
    assert row.intent == "review"
    assert row.channel == "slack"
    assert (
        row.status == "awaiting_confirmation"
    )  # not pinged until the requester confirms
    assert row.request_key == d.request_key
    # The origin thread context is stashed so the approved review lands back where it was asked.
    assert row.slack_channel == "C1" and row.slack_thread_ts == "1.0"


def test_duplicate_re_request_collapses_and_never_pings_at_gate(
    bot_session_factory,
) -> None:
    # A gate miss NEVER pings the admin now — it stashes an awaiting_confirmation row and the requester
    # must confirm. A re-ask collapses onto the same row (no duplicate).
    post_blocks = _CapturePostBlocks()
    notifier = AdminNotifier(post_blocks=post_blocks)
    st = _settings(nda_admin_slack_channel="C_ADMIN")
    env = _slack_env("U9")
    with bot_session_factory() as s:
        d1 = gate_check(s, env, "review", settings=st, notifier=notifier)
    with bot_session_factory() as s:
        d2 = gate_check(s, env, "review", settings=st, notifier=notifier)
    assert d1.status == d2.status == "needs_confirmation"
    assert d1.request_key == d2.request_key  # stable per sender+intent
    assert len(_pending_rows(bot_session_factory)) == 1  # collapsed — no duplicate row
    assert post_blocks.calls == []  # NEVER pinged at the gate (only on confirm)


def test_advance_transitions_and_notifies_once(bot_session_factory) -> None:
    # The confirm step (advance_and_notify) is what actually pings the admin — exactly once, idempotent.
    post_blocks = _CapturePostBlocks()
    notifier = AdminNotifier(post_blocks=post_blocks)
    st = _settings(nda_admin_slack_channel="C_ADMIN")
    with bot_session_factory() as s:
        d = gate_check(s, _slack_env("U9"), "review", settings=st)
    with bot_session_factory() as s:
        r1 = advance_and_notify(s, d.request_key, notifier=notifier, settings=st)
        s.commit()
    with bot_session_factory() as s:
        r2 = advance_and_notify(s, d.request_key, notifier=notifier, settings=st)
        s.commit()
    assert r1 == "notified"
    assert r2 == "already"  # idempotent — a second advance never re-pings
    assert len(post_blocks.calls) == 1
    with bot_session_factory() as s:
        row = s.query(NdaPendingRequest).filter_by(request_key=d.request_key).one()
        assert row.status == "pending"


def test_advance_is_fail_closed_when_clicker_is_not_the_requester(
    bot_session_factory,
) -> None:
    # advance_and_notify with requester_id set MUST match the row's requester, else it refuses + no ping.
    post_blocks = _CapturePostBlocks()
    notifier = AdminNotifier(post_blocks=post_blocks)
    st = _settings(nda_admin_slack_channel="C_ADMIN")
    with bot_session_factory() as s:
        d = gate_check(s, _slack_env("U9"), "review", settings=st)
    with bot_session_factory() as s:
        result = advance_and_notify(
            s, d.request_key, notifier=notifier, settings=st, requester_id="U_OTHER"
        )
        s.commit()
    assert result == "forbidden"
    assert post_blocks.calls == []  # the bystander's click pinged nobody
    with bot_session_factory() as s:
        row = s.query(NdaPendingRequest).filter_by(request_key=d.request_key).one()
        assert row.status == "awaiting_confirmation"  # unchanged


def test_distinct_intents_get_distinct_pending_rows(bot_session_factory) -> None:
    env_r = _slack_env("U9", text="review this")
    env_e = _slack_env("U9", text="send to docusign")
    with bot_session_factory() as s:
        dr = gate_check(s, env_r, "review", settings=_settings())
    with bot_session_factory() as s:
        de = gate_check(s, env_e, "envelope", settings=_settings())
    assert dr.request_key != de.request_key
    assert len(_pending_rows(bot_session_factory)) == 2


# --------------------------------------------------------------------------- #
# ADMIN NOTIFY — payload shape (captured sinks)
# --------------------------------------------------------------------------- #
def test_admin_notify_slack_block_kit_payload_shape(bot_session_factory) -> None:
    post_blocks = _CapturePostBlocks()
    notifier = AdminNotifier(post_blocks=post_blocks)
    st = _settings(nda_admin_slack_channel="C_ADMIN")
    with bot_session_factory() as s:
        d = gate_check(s, _slack_env("U9"), "review", settings=st)
    with bot_session_factory() as s:
        advance_and_notify(s, d.request_key, notifier=notifier, settings=st)
        s.commit()

    assert len(post_blocks.calls) == 1
    admin_env, blocks, fallback = post_blocks.calls[0]
    # Posted to the ADMIN channel (not the requester's channel), at the channel root (no thread).
    assert admin_env.channel == "slack"
    assert admin_env.slack_channel == "C_ADMIN"
    assert admin_env.slack_thread_ts == ""
    # The ported "Notify Admin" fallback text (reference §3.5).
    assert (
        fallback
        == f"Approval requested: U9 wants to run review. Request {d.request_key}."
    )
    # An actions block with primary Approve + danger Deny buttons carrying the versioned typed values.
    actions = [b for b in blocks if b.get("type") == "actions"][0]
    buttons = actions["elements"]
    assert [b["action_id"] for b in buttons] == [
        ACTION_APPROVAL_APPROVE,
        ACTION_APPROVAL_DENY,
    ]
    assert [b["style"] for b in buttons] == ["primary", "danger"]
    approve_val = json.loads(buttons[0]["value"])
    deny_val = json.loads(buttons[1]["value"])
    assert approve_val == {
        "v": 1,
        "kind": "approval",
        "request_key": d.request_key,
        "action": "approve",
    }
    assert deny_val == {
        "v": 1,
        "kind": "approval",
        "request_key": d.request_key,
        "action": "deny",
    }


def test_admin_notify_email_fallback_when_slack_not_wired(bot_session_factory) -> None:
    service = _CaptureService()
    # Slack NOT wired (post_blocks is None) -> the plain-email fallback to NDA_ADMIN_EMAIL is used.
    notifier = AdminNotifier(service=service, post_blocks=None)
    st = _settings(nda_admin_slack_channel="C_ADMIN", nda_admin_email="admin@x.com")
    with bot_session_factory() as s:
        d = gate_check(s, _slack_env("U9"), "envelope", settings=st)
    with bot_session_factory() as s:
        advance_and_notify(s, d.request_key, notifier=notifier, settings=st)
        s.commit()

    assert len(service.calls) == 1
    admin_env, reply = service.calls[0]
    assert admin_env.channel == "email"
    assert (
        admin_env.sender_address == "admin@x.com"
    )  # EmailReplySink sends To: sender_address
    assert (
        reply.text
        == f"Approval requested: U9 wants to run envelope. Request {d.request_key}."
    )


def test_pending_persists_even_when_no_admin_channel_configured(
    bot_session_factory,
) -> None:
    # No Slack, no admin email — the request is still recorded (awaiting_confirmation) even though a
    # later advance would have no channel to notify.
    with bot_session_factory() as s:
        d = gate_check(s, _slack_env("U9"), "review", settings=_settings())
    assert d.status == "needs_confirmation"
    rows = _pending_rows(bot_session_factory)
    assert len(rows) == 1 and rows[0].status == "awaiting_confirmation"


# --------------------------------------------------------------------------- #
# APPROVE / DENY — idempotent transitions + allowlist add
# --------------------------------------------------------------------------- #
def test_approve_adds_allowlist_row_and_is_idempotent(bot_session_factory) -> None:
    with bot_session_factory() as s:
        d = gate_check(s, _slack_env("U9"), "review", settings=_settings())
    rk = d.request_key

    with bot_session_factory() as s:
        assert approve_request(s, rk, "admin1") is True
    # The principal is now on the allowlist and the request is 'approved'.
    rows = _allowlist_rows(bot_session_factory)
    assert len(rows) == 1 and (rows[0].principal_type, rows[0].principal_key) == (
        "slack",
        "U9",
    )
    assert rows[0].added_by == "admin1"
    with bot_session_factory() as s:
        pr = s.query(NdaPendingRequest).filter_by(request_key=rk).one()
        assert (
            pr.status == "approved"
            and pr.decided_by == "admin1"
            and pr.decided_at is not None
        )

    # Idempotent: a second approve neither errors nor duplicates the allowlist row.
    with bot_session_factory() as s:
        assert approve_request(s, rk, "admin1") is True
    assert len(_allowlist_rows(bot_session_factory)) == 1


def test_approve_lets_the_retry_pass_the_gate(bot_session_factory) -> None:
    with bot_session_factory() as s:
        d = gate_check(s, _slack_env("U9"), "review", settings=_settings())
    with bot_session_factory() as s:
        approve_request(s, d.request_key, "admin1")
    # The user retries — now ALLOWED (this wave requires a retry; auto-resume is a later enhancement).
    with bot_session_factory() as s:
        d2 = gate_check(s, _slack_env("U9"), "review", settings=_settings())
    assert d2.status == "allowed"


def test_email_approval_still_requires_verification_on_retry(
    bot_session_factory,
) -> None:
    # An UNVERIFIED email is refused; approving adds ('email', addr) — but the retry is allowed ONLY when
    # the retry is itself DMARC-verified. An unverified retry stays pending even after approval (§3.3/§6).
    with bot_session_factory() as s:
        d = gate_check(
            s, _email_env("jane@x.com", verified=False), "review", settings=_settings()
        )
    with bot_session_factory() as s:
        assert approve_request(s, d.request_key, "admin1") is True
    rows = _allowlist_rows(bot_session_factory)
    assert (rows[0].principal_type, rows[0].principal_key) == ("email", "jane@x.com")

    with bot_session_factory() as s:  # unverified retry -> still refused
        du = gate_check(
            s, _email_env("jane@x.com", verified=False), "review", settings=_settings()
        )
    assert du.status == "pending"
    with bot_session_factory() as s:  # verified retry -> allowed
        dv = gate_check(
            s, _email_env("jane@x.com", verified=True), "review", settings=_settings()
        )
    assert dv.status == "allowed"


def test_deny_is_idempotent_and_never_allows(bot_session_factory) -> None:
    with bot_session_factory() as s:
        d = gate_check(s, _slack_env("U9"), "envelope", settings=_settings())
    rk = d.request_key

    with bot_session_factory() as s:
        assert deny_request(s, rk, "admin1") is True
    with bot_session_factory() as s:
        assert deny_request(s, rk, "admin1") is True  # idempotent
    with bot_session_factory() as s:
        pr = s.query(NdaPendingRequest).filter_by(request_key=rk).one()
        assert pr.status == "denied"
    assert (
        _allowlist_rows(bot_session_factory) == []
    )  # deny never touches the allowlist
    # A denied request still refuses on retry.
    with bot_session_factory() as s:
        d2 = gate_check(s, _slack_env("U9"), "envelope", settings=_settings())
    assert d2.status == "pending"


def test_approve_then_deny_conflict_and_missing_request(bot_session_factory) -> None:
    with bot_session_factory() as s:
        d = gate_check(s, _slack_env("U9"), "review", settings=_settings())
    rk = d.request_key
    with bot_session_factory() as s:
        assert approve_request(s, rk, "admin1") is True
    with bot_session_factory() as s:
        assert (
            deny_request(s, rk, "admin1") is False
        )  # cannot deny an already-approved request
    # Unknown request keys are a no-op False on both transitions.
    with bot_session_factory() as s:
        assert approve_request(s, "req_does_not_exist", "admin1") is False
        assert deny_request(s, "req_does_not_exist", "admin1") is False


# --------------------------------------------------------------------------- #
# FAIL CLOSED — a DB / session error must NEVER return allowed
# --------------------------------------------------------------------------- #
class _BoomSession:
    """A session whose every query raises — stands in for a DB read failure mid-gate."""

    def execute(self, *a: object, **k: object) -> object:
        raise RuntimeError("db read failed")

    def commit(self) -> None:  # pragma: no cover - not reached before the read blows up
        raise RuntimeError("db commit failed")

    def rollback(self) -> None:
        pass


def test_gate_check_fails_closed_on_db_read_error() -> None:
    # A verified, would-be-allowed identity: if the membership read throws, the gate must REFUSE, not
    # optimistically allow. Never touches a real DB (the fake session raises on execute).
    d = gate_check(_BoomSession(), _slack_env("U9"), "review", settings=_settings())  # type: ignore[arg-type]
    assert d.status == "pending"  # fail CLOSED — NEVER "allowed"


def test_allowlist_gate_fails_closed_on_session_open_error() -> None:
    def _boom_factory():
        raise RuntimeError("cannot open a session")

    gate = AllowlistGate(session_factory=_boom_factory, settings=_settings())  # type: ignore[arg-type]
    d = gate.check(_slack_env("U9"), Classification(intent="review"))
    assert d.status == "pending"  # session failure -> refuse, never allow


def test_allowlist_gate_non_gated_intent_allowed_without_touching_db() -> None:
    # template/generate/help/archive stay OPEN — the gate must not even open a session for them.
    def _boom_factory():  # would raise if the gate touched the DB
        raise AssertionError("non-gated intent must not open a session")

    gate = AllowlistGate(session_factory=_boom_factory, settings=_settings())  # type: ignore[arg-type]
    for intent in ("template", "generate", "help", "archive"):
        assert (
            gate.check(_slack_env("U9"), Classification(intent=intent)).status
            == "allowed"
        )


def test_allowlist_gate_allows_verified_allowlisted_principal(
    bot_session_factory,
) -> None:
    # End-to-end through the router-facing gate object (opens its own session via the factory).
    _seed_allowlist(bot_session_factory, "slack", "U9")
    gate = AllowlistGate(session_factory=bot_session_factory, settings=_settings())
    d = gate.check(_slack_env("U9"), Classification(intent="review"))
    assert d.status == "allowed"


def test_allowlist_gate_miss_needs_confirmation_then_advance_notifies(
    bot_session_factory,
) -> None:
    post_blocks = _CapturePostBlocks()
    gate = AllowlistGate(
        session_factory=bot_session_factory,
        settings=_settings(nda_admin_slack_channel="C_ADMIN"),
        notifier=AdminNotifier(post_blocks=post_blocks),
    )
    # A miss stashes the request (awaiting_confirmation) and does NOT ping the admin yet.
    d = gate.check(_slack_env("U9"), Classification(intent="envelope"))
    assert d.status == "needs_confirmation"
    assert len(_pending_rows(bot_session_factory)) == 1
    assert post_blocks.calls == []
    # The gate's own advance (the router's email auto-advance path) transitions + pings, exactly once.
    assert gate.advance(d.request_key) == "notified"
    assert gate.advance(d.request_key) == "already"
    assert len(post_blocks.calls) == 1
    assert _pending_rows(bot_session_factory)[0].status == "pending"
