"""Admin-plane hardening pins (PLAN §6).

The ported password plane already implements login lockout, per-IP throttling, anti-enumeration, and
single-use time-boxed reset tokens (characterized in ``test_auth_routes.py``). This suite PINS the
security-critical invariants the wave-A hardening either verifies or newly adds:

  1. Lockout matrix — per-user threshold/expiry, per-IP window expiry (deterministic, injected clock).
  2. No-oracle proofs — login (unknown user vs wrong password) and reset (known vs unknown, WITH email
     delivery enabled) return BYTE-IDENTICAL bodies.
  3. Reset email wired to SMTP — captured through a fake transport (zero network): the token is
     delivered, is single-use, and expires; email_out OFF is a safe no-op.
  4. Session cookie flags — Secure / HttpOnly / SameSite=Strict (PLAN §6), must-change-password intact.
  5. Optional admin IP allowlist dependency — allow-by-default, exact + CIDR, spoofed XFF ignored
     without a trusted edge.

Zero network: the SMTP transport is faked; no AI/provider calls are made.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api.errors import EngineError
from tests.conftest import PASSWORD

NEW_PASSWORD = "brand-new-pass-123"


# --------------------------------------------------------------------------- #
# Fake SMTP transport (the "fake sink" — captures outbound messages, no network)
# --------------------------------------------------------------------------- #
class _FakeSMTP:
    def __init__(self, outbox: list) -> None:
        self._outbox = outbox

    def send_message(self, msg) -> None:
        self._outbox.append(msg)


def _install_fake_sink(monkeypatch) -> list:
    """Swap the module-global reset sender for one whose transport captures the EmailMessage. Returns
    the outbox list. monkeypatch reverts it after the test."""
    from app.api import routes_auth
    from app.auth.reset_email import ResetEmailSender

    outbox: list = []

    @contextmanager
    def _factory(_settings):
        yield _FakeSMTP(outbox)

    monkeypatch.setattr(
        routes_auth,
        "_reset_email_sender",
        ResetEmailSender(transport_factory=_factory),
    )
    return outbox


def _enable_email_out(app, monkeypatch) -> None:
    """Turn the email_out capability ENABLED for this per-test app (set SMTP creds + re-derive)."""
    from app.capabilities import EMAIL_OUT
    from app.config import settings

    monkeypatch.setattr(settings, "smtp_host", "smtp.example.test", raising=False)
    monkeypatch.setattr(settings, "smtp_user", "bot@example.test", raising=False)
    monkeypatch.setattr(settings, "smtp_password", "secret", raising=False)
    app.state.capabilities.mark_recovered(EMAIL_OUT)


def _set_email(db, user, email: str) -> None:
    user.email = email
    db.add(user)
    db.commit()


def _token_from_message(msg) -> str:
    body = msg.get_body(preferencelist=("plain",))
    text = body.get_content()
    m = re.search(r"token=([A-Za-z0-9_\-]+)", text)
    assert m, f"no reset token in email body: {text!r}"
    return m.group(1)


def _set_cookie_for(resp, name: str) -> str | None:
    for raw in resp.headers.get_list("set-cookie"):
        if raw.startswith(f"{name}="):
            return raw
    return None


# --------------------------------------------------------------------------- #
# 1a. Per-user lockout matrix
# --------------------------------------------------------------------------- #
def test_per_user_lockout_expiry_lets_correct_password_through(
    client, db, seed_user, session_factory
):
    """A lock whose ``locked_until`` is already in the PAST is not enforced: the next login checks
    (and clears) it, so a correct password succeeds and the failed-count resets to 0."""
    from app.auth.models import UserAccount

    user = seed_user("alice")
    user.failed_login_count = 5
    user.locked_until = datetime.now(UTC) - timedelta(minutes=1)
    db.add(user)
    db.commit()

    r = client.post("/api/auth/login", json={"user_id": "alice", "password": PASSWORD})
    assert r.status_code == 200

    s = session_factory()
    try:
        fresh = s.get(UserAccount, user.id)
        assert fresh.failed_login_count == 0
        assert fresh.locked_until is None
    finally:
        s.close()


def test_per_user_lock_in_future_blocks_even_correct_password(client, db, seed_user):
    """A ``locked_until`` in the future is enforced BEFORE the password is verified: even the correct
    password is refused with 423 (pins the lock mechanism directly, not just the 5-fail trigger)."""
    user = seed_user("alice")
    user.locked_until = datetime.now(UTC) + timedelta(minutes=10)
    db.add(user)
    db.commit()

    r = client.post("/api/auth/login", json={"user_id": "alice", "password": PASSWORD})
    assert r.status_code == 423
    assert r.json()["error"]["code"] == "account_locked"


# --------------------------------------------------------------------------- #
# 1b. Per-IP sliding-window: limit + WINDOW EXPIRY (deterministic injected clock)
# --------------------------------------------------------------------------- #
def test_sliding_window_blocks_at_limit_then_expires_after_window():
    """``blocked`` peeks without consuming: it reports blocked once ``limit`` hits are inside the
    window and clears exactly when the oldest hit ages past ``window_s`` (injected ``now``)."""
    from app.api.routes_auth import _SlidingWindow

    w = _SlidingWindow()
    for _ in range(3):
        w.record("ip1", 300.0, now=0.0)

    assert w.blocked("ip1", 3, 300.0, now=0.0) is not None  # at limit
    assert w.blocked("ip1", 3, 300.0, now=299.0) is not None  # still within window
    assert w.blocked("ip1", 3, 300.0, now=301.0) is None  # oldest hit aged out


def test_sliding_window_allow_consumes_and_rejects_without_recording():
    """``allow`` (the reset-request path) is check-and-consume: an admitted call records a hit, a
    rejected call does NOT (so retrying immediately never extends the block), and budget frees after
    the window."""
    from app.api.routes_auth import _SlidingWindow

    w = _SlidingWindow()
    assert w.allow("ip", 2, 300.0, now=0.0) is None  # 1st admitted
    assert w.allow("ip", 2, 300.0, now=0.0) is None  # 2nd admitted
    assert w.allow("ip", 2, 300.0, now=0.0) is not None  # 3rd rejected (over limit)
    # The rejected call did not consume a slot, and after the window the counter frees:
    assert w.allow("ip", 2, 300.0, now=301.0) is None


# --------------------------------------------------------------------------- #
# 2. No-oracle proofs (byte-identical bodies)
# --------------------------------------------------------------------------- #
def test_login_unknown_user_and_wrong_password_are_byte_identical(client, seed_user):
    """An unknown user_id and a wrong password for a real user return the SAME 401 with a
    byte-identical body — no account-existence oracle."""
    seed_user("alice")
    r_wrong = client.post(
        "/api/auth/login", json={"user_id": "alice", "password": "not-the-password"}
    )
    r_unknown = client.post(
        "/api/auth/login", json={"user_id": "ghost-who-never-existed", "password": "x"}
    )
    assert r_wrong.status_code == 401
    assert r_unknown.status_code == 401
    assert r_wrong.json()["error"]["code"] == "invalid_credentials"
    assert r_wrong.content == r_unknown.content  # byte-identical body


def test_reset_request_byte_identical_body_with_email_enabled(
    client, app, db, seed_user, monkeypatch
):
    """Even with real email delivery ON, a known vs unknown user_id return byte-identical 200 bodies —
    and the send happens ONLY for the real account (captured), so delivery never leaks existence into
    the response (it runs in the background, after the response is flushed)."""
    outbox = _install_fake_sink(monkeypatch)
    _enable_email_out(app, monkeypatch)
    user = seed_user("alice")
    _set_email(db, user, "alice@example.test")

    r_known = client.post("/api/auth/password/reset-request", json={"user_id": "alice"})
    r_unknown = client.post(
        "/api/auth/password/reset-request", json={"user_id": "ghost-who-never-existed"}
    )
    assert r_known.status_code == 200
    assert r_unknown.status_code == 200
    assert r_known.content == r_unknown.content  # byte-identical body

    # Exactly one email — for the real account — with a clean, non-reply, non-reflecting shape.
    assert len(outbox) == 1
    msg = outbox[0]
    assert msg["To"] == "alice@example.test"
    from app.auth.reset_email import RESET_SUBJECT

    assert msg["Subject"] == RESET_SUBJECT
    assert msg["In-Reply-To"] is None  # fresh message, not a reply
    assert "alice" not in msg["Subject"]  # no user-controlled reflection in the subject


# --------------------------------------------------------------------------- #
# 3. Reset email wired to SMTP: delivery, single-use, expiry, capability no-op
# --------------------------------------------------------------------------- #
def test_reset_email_delivers_token_and_is_single_use(
    client, app, db, seed_user, monkeypatch
):
    """End-to-end through the fake sink: the delivered token completes ONE reset, then is refused on
    reuse (single-use); the new password then authenticates and the old one does not."""
    outbox = _install_fake_sink(monkeypatch)
    _enable_email_out(app, monkeypatch)
    user = seed_user("alice")
    _set_email(db, user, "alice@example.test")

    r = client.post("/api/auth/password/reset-request", json={"user_id": "alice"})
    assert r.status_code == 200
    assert len(outbox) == 1
    token = _token_from_message(outbox[0])

    # First use succeeds.
    ok = client.post(
        "/api/auth/password/reset-confirm",
        json={"token": token, "new_password": NEW_PASSWORD},
    )
    assert ok.status_code == 200

    # Second use of the same token is refused (single-use).
    reuse = client.post(
        "/api/auth/password/reset-confirm",
        json={"token": token, "new_password": "another-pass-456"},
    )
    assert reuse.status_code == 400
    assert reuse.json()["error"]["code"] == "invalid_token"

    # New password authenticates; the old one does not.
    assert (
        client.post(
            "/api/auth/login", json={"user_id": "alice", "password": NEW_PASSWORD}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/auth/login", json={"user_id": "alice", "password": PASSWORD}
        ).status_code
        == 401
    )


def test_reset_confirm_expired_token_is_400(client, db, seed_user):
    """A token whose ``expires_at`` has passed is rejected (time-boxed). Seeded directly so no clock
    manipulation is needed."""
    from app.auth.models import PasswordResetToken
    from app.auth.sessions import hash_token

    user = seed_user("alice")
    raw = "expired-raw-token-value-000"
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hash_token(raw),
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    db.commit()

    r = client.post(
        "/api/auth/password/reset-confirm",
        json={"token": raw, "new_password": NEW_PASSWORD},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_token"


def test_reset_confirm_consumes_all_outstanding_tokens(client, db, seed_user):
    """A successful reset invalidates EVERY outstanding token for the user, not just the one used — a
    previously-issued (still-unexpired) token is dead afterward."""
    from app.auth.models import PasswordResetToken
    from app.auth.sessions import hash_token

    user = seed_user("alice")
    raw1, raw2 = "raw-token-one-aaa", "raw-token-two-bbb"
    for raw in (raw1, raw2):
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_token(raw),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    db.commit()

    first = client.post(
        "/api/auth/password/reset-confirm",
        json={"token": raw1, "new_password": NEW_PASSWORD},
    )
    assert first.status_code == 200

    second = client.post(
        "/api/auth/password/reset-confirm",
        json={"token": raw2, "new_password": "another-pass-456"},
    )
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "invalid_token"


def test_reset_request_email_disabled_is_safe_noop(
    client, app, db, seed_user, monkeypatch
):
    """email_out capability OFF -> the ported safe no-op: still 200 with the identical anti-enumeration
    body, and NO send occurs (the fake sink is installed but never invoked)."""
    from app.capabilities import EMAIL_OUT, CapabilityState

    outbox = _install_fake_sink(monkeypatch)
    assert app.state.capabilities.state(EMAIL_OUT) is CapabilityState.DISABLED

    user = seed_user("alice")
    _set_email(db, user, "alice@example.test")

    r_known = client.post("/api/auth/password/reset-request", json={"user_id": "alice"})
    r_unknown = client.post(
        "/api/auth/password/reset-request", json={"user_id": "ghost"}
    )
    assert r_known.status_code == 200
    assert r_known.content == r_unknown.content
    assert outbox == []  # capability off -> nothing delivered


def test_reset_email_sender_reflects_no_user_content_and_is_fresh(monkeypatch):
    """Unit pin: the built message carries fixed copy + only the server-minted reset link (no threading
    headers, no user-controlled reflection)."""
    from app.auth.reset_email import RESET_SUBJECT, build_reset_message
    from app.config import settings

    monkeypatch.setattr(
        settings, "nda_bot_from_email", "nda-bot@example.test", raising=False
    )
    msg = build_reset_message(
        settings,
        to_email="user@example.test",
        reset_link="https://nda.example.test/reset-password?token=abc123",
    )
    assert msg["Subject"] == RESET_SUBJECT
    assert msg["To"] == "user@example.test"
    assert msg["In-Reply-To"] is None and msg["References"] is None
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert "abc123" in body  # the server-minted token link is present


# --------------------------------------------------------------------------- #
# 4. Session cookie flags (PLAN §6: Secure / HttpOnly / SameSite=Strict)
# --------------------------------------------------------------------------- #
def test_login_cookies_are_httponly_and_samesite_strict(client, seed_user):
    """The session cookie is HttpOnly + SameSite=Strict + Path=/; the CSRF cookie is SameSite=Strict
    but readable by the SPA (NOT HttpOnly) for the double-submit check."""
    seed_user("alice")
    r = client.post("/api/auth/login", json={"user_id": "alice", "password": PASSWORD})
    assert r.status_code == 200

    sid = (_set_cookie_for(r, "sid") or "").lower()
    csrf = (_set_cookie_for(r, "csrf") or "").lower()
    assert sid and csrf
    assert "httponly" in sid
    assert "samesite=strict" in sid
    assert "path=/" in sid
    assert "samesite=strict" in csrf
    assert "httponly" not in csrf  # SPA must read it (double-submit)


def test_login_client_ip_ignores_spoofed_xff_without_trusted_edge(monkeypatch):
    """The login/reset throttle IP must NOT trust a client-supplied X-Forwarded-For unless a trusted
    edge is declared — else an attacker rotating the header gets a fresh throttle bucket per request
    and bypasses the per-IP rate limit entirely (final-review finding, fixed via the canonical
    resolver). With trust off, the direct socket peer wins; with trust on, the first XFF hop wins."""
    from app.api.routes_auth import _client_ip
    from app.config import settings

    class _Req:
        def __init__(self, headers, host):
            self.headers = headers
            self.client = type("C", (), {"host": host})()

    spoofed = _Req({"x-forwarded-for": "1.2.3.4"}, host="203.0.113.9")

    monkeypatch.setattr(settings, "trust_forwarded_proto", False, raising=False)
    assert _client_ip(spoofed) == "203.0.113.9"  # spoofed header ignored -> real peer

    monkeypatch.setattr(settings, "trust_forwarded_proto", True, raising=False)
    assert _client_ip(spoofed) == "1.2.3.4"  # trusted edge -> first XFF hop


def test_login_cookies_secure_flag_tracks_scheme(client, seed_user):
    """Secure is set when the request is https (here via a trusted X-Forwarded-Proto) and OFF over
    plain http so the cookie stays usable in dev/tests."""
    seed_user("alice")

    plain = client.post(
        "/api/auth/login", json={"user_id": "alice", "password": PASSWORD}
    )
    assert "secure" not in (_set_cookie_for(plain, "sid") or "").lower()

    fwd = client.post(
        "/api/auth/login",
        json={"user_id": "alice", "password": PASSWORD},
        headers={"x-forwarded-proto": "https"},
    )
    assert "secure" in (_set_cookie_for(fwd, "sid") or "").lower()


def test_must_change_password_flow_intact(client, seed_user):
    """The must-change-password flow still works through the hardened cookies: login surfaces the
    flag, the (ungated) change endpoint clears it, and a fresh login reflects the cleared state."""
    seed_user("alice", must_change_password=True)
    r = client.post("/api/auth/login", json={"user_id": "alice", "password": PASSWORD})
    assert r.status_code == 200
    assert r.json()["must_change_password"] is True

    csrf = client.cookies.get("csrf")
    assert client.get("/api/auth/me").json()["must_change_password"] is True

    changed = client.post(
        "/api/auth/password/change",
        json={"old_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers={"x-csrf-token": csrf},
    )
    assert changed.status_code == 200

    fresh = client.post(
        "/api/auth/login", json={"user_id": "alice", "password": NEW_PASSWORD}
    )
    assert fresh.json()["must_change_password"] is False


# --------------------------------------------------------------------------- #
# 5. Optional admin IP allowlist dependency
# --------------------------------------------------------------------------- #
class _StubReq:
    def __init__(self, headers=None, host="203.0.113.9"):
        self.headers = headers or {}
        self.client = SimpleNamespace(host=host)


def test_parse_allowlist_accepts_list_and_delimited_string():
    from app.auth.admin_ip import parse_allowlist

    assert parse_allowlist(None) == ()
    assert parse_allowlist("") == ()
    assert parse_allowlist("10.0.0.1, 10.0.0.2") == ("10.0.0.1", "10.0.0.2")
    assert parse_allowlist("10.0.0.1 10.0.0.2;10.0.0.3") == (
        "10.0.0.1",
        "10.0.0.2",
        "10.0.0.3",
    )
    assert parse_allowlist(["10.0.0.1", "  ", "10.0.0.2"]) == ("10.0.0.1", "10.0.0.2")


def test_ip_allowed_exact_cidr_and_malformed():
    from app.auth.admin_ip import ip_allowed

    assert ip_allowed("10.0.0.5", ["10.0.0.5"])
    assert ip_allowed("10.0.0.5", ["10.0.0.0/24"])
    assert not ip_allowed("10.0.1.5", ["10.0.0.0/24"])
    assert not ip_allowed(
        "10.0.0.5", ["not-an-ip"]
    )  # malformed entry skipped, no crash
    assert not ip_allowed("garbage", ["10.0.0.0/24"])  # malformed client ip
    assert not ip_allowed("", ["10.0.0.0/24"])


def test_configured_allowlist_reads_settings_field():
    """``configured_allowlist`` reads the real ``admin_ip_allowlist`` Settings field (P6). A stub cfg
    still works too (the read is a plain ``getattr``), so a caller can pass any object."""
    from app.auth.admin_ip import configured_allowlist

    assert configured_allowlist(
        SimpleNamespace(admin_ip_allowlist="10.0.0.1, 10.0.0.2")
    ) == (
        "10.0.0.1",
        "10.0.0.2",
    )
    assert configured_allowlist(SimpleNamespace()) == ()  # unset stub -> empty


def test_configured_allowlist_enable_matrix_on_real_settings():
    """The real Settings field drives the gate: empty (default) => allow-all pass-through; a set value
    (IPs and/or CIDRs, any of comma/space/semicolon delimited) => that parsed allowlist."""
    from app.auth.admin_ip import configured_allowlist
    from app.config import Settings

    # Default: empty -> allow-all (the gate is a no-op).
    assert configured_allowlist(Settings(_env_file=None)) == ()
    # Set: a mixed IP + CIDR list, comma/space/semicolon delimited, parses to the tuple.
    cfg = Settings(
        _env_file=None, admin_ip_allowlist="10.0.0.1, 192.168.0.0/16;172.16.0.5"
    )
    assert configured_allowlist(cfg) == ("10.0.0.1", "192.168.0.0/16", "172.16.0.5")


def test_require_admin_ip_allows_when_unset(monkeypatch):
    """With no allowlist configured (the default empty ``admin_ip_allowlist``), the dependency is a
    transparent pass-through."""
    from app.auth import admin_ip

    # The real configured_allowlist returns () for the default empty field; assert that path directly.
    assert admin_ip.require_admin_ip(_StubReq()) is None


def test_require_admin_ip_enforces_when_set(monkeypatch):
    from app.auth import admin_ip
    from app.config import settings

    # Simulate the future field via the allowlist read seam (Settings forbids extra fields).
    monkeypatch.setattr(
        admin_ip, "configured_allowlist", lambda cfg=None: ("10.0.0.0/24",)
    )
    monkeypatch.setattr(settings, "trust_forwarded_proto", True, raising=False)

    # On-list (via trusted XFF) -> allowed.
    assert (
        admin_ip.require_admin_ip(_StubReq(headers={"x-forwarded-for": "10.0.0.42"}))
        is None
    )

    # Off-list -> 403 through the standard error taxonomy.
    with pytest.raises(EngineError) as ei:
        admin_ip.require_admin_ip(_StubReq(headers={"x-forwarded-for": "192.168.1.1"}))
    assert ei.value.status == 403
    assert ei.value.code == "admin_ip_forbidden"


def test_require_admin_ip_ignores_spoofed_xff_without_trusted_edge(monkeypatch):
    """Without a trusted edge the client-supplied X-Forwarded-For is NOT honoured — the direct peer
    is used, so a spoofed header can't fake an allowlisted address."""
    from app.auth import admin_ip
    from app.config import settings

    monkeypatch.setattr(
        admin_ip, "configured_allowlist", lambda cfg=None: ("10.0.0.0/24",)
    )
    monkeypatch.setattr(settings, "trust_forwarded_proto", False, raising=False)

    with pytest.raises(EngineError) as ei:
        admin_ip.require_admin_ip(
            _StubReq(headers={"x-forwarded-for": "10.0.0.42"}, host="203.0.113.9")
        )
    assert ei.value.status == 403  # peer 203.0.113.9 is off-list; spoofed XFF ignored


def test_require_admin_ip_renders_through_envelope_as_dependency(
    client, app, monkeypatch
):
    """Wired as a real FastAPI dependency it renders a 403 through the shared error envelope, and
    passes when unset or on-list (the wave-B admin pages compose this after require_admin)."""
    from fastapi import Depends

    from app.auth import admin_ip
    from app.auth.admin_ip import require_admin_ip

    app.add_api_route(
        "/_probe/admin-ip",
        lambda: {"ok": True},
        dependencies=[Depends(require_admin_ip)],
        methods=["GET"],
    )
    # The app's catch-all default-deny 404 is registered LAST and matches "/{path:path}"; move our
    # just-appended probe route in FRONT of it so it is matched first.
    app.router.routes.insert(0, app.router.routes.pop())

    assert client.get("/_probe/admin-ip").status_code == 200  # unset -> allow

    monkeypatch.setattr(
        admin_ip, "configured_allowlist", lambda cfg=None: ("10.0.0.0/24",)
    )
    denied = client.get("/_probe/admin-ip", headers={"x-forwarded-for": "192.0.2.1"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "admin_ip_forbidden"

    allowed = client.get("/_probe/admin-ip", headers={"x-forwarded-for": "10.0.0.42"})
    assert allowed.status_code == 200
