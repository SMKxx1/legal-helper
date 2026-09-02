"""Characterization tests for the /api/auth public surface beyond the smoke tests.

These exercise the REAL app (CSRF middleware + per-route ``require_csrf`` dependency +
the error-envelope handler) against a throwaway DB. We deliberately do NOT re-cover the
basic login success/failure or /me cases (those live in test_smoke.py). What's here:

  - brute-force lockout (5 failed logins -> 423 account_locked),
  - logout CSRF gate (missing header -> 403; valid header -> 200 + session invalidated),
  - password change (wrong old -> 400; correct -> 200 + old session rotated out),
  - reset-request anti-enumeration (identical 200 body for known & unknown users),
  - reset-confirm with a bad token -> 400 invalid_token.

No network / AI / provider calls are made; password reset email delivery is a documented
STUB in the app, so we only assert the token-less / gate behavior.
"""

from __future__ import annotations

from tests.conftest import PASSWORD


# --------------------------------------------------------------------------- #
# Brute-force lockout
# --------------------------------------------------------------------------- #
def test_five_failed_logins_locks_account_with_423(client, seed_user, login):
    """The 5th consecutive wrong password trips the lockout: that request itself
    returns 423 account_locked (MAX_FAILED_LOGINS = 5)."""
    seed_user("alice")
    # First four wrong attempts stay the generic 401.
    for _ in range(4):
        r = login("alice", password="nope")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "invalid_credentials"
    # The fifth wrong attempt crosses the threshold and locks.
    r = login("alice", password="nope")
    assert r.status_code == 423
    assert r.json()["error"]["code"] == "account_locked"


def test_locked_account_rejects_even_correct_password(client, seed_user, login):
    """Once locked, the correct password is still refused with 423 (the lock is
    checked BEFORE the password is verified)."""
    seed_user("alice")
    for _ in range(5):
        login("alice", password="nope")
    r = login("alice", password=PASSWORD)
    assert r.status_code == 423
    assert r.json()["error"]["code"] == "account_locked"


# --------------------------------------------------------------------------- #
# Logout CSRF gate
# --------------------------------------------------------------------------- #
def test_logout_without_csrf_header_is_403(client, seed_user, login):
    """Logged in (session cookie present) but no x-csrf-token -> 403 csrf_failed.
    The login fixture installs the CSRF header by default, so override it to empty
    for this single request to simulate a missing/forged header."""
    seed_user("alice")
    login("alice")
    r = client.post("/api/auth/logout", headers={"x-csrf-token": ""})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "csrf_failed"


def test_logout_with_csrf_succeeds_and_invalidates_session(client, seed_user, login):
    """With the matching CSRF header (set by the login fixture) logout returns
    200 {"ok": true} and the session is no longer valid for /me afterward."""
    seed_user("alice")
    login("alice")
    # Sanity: the session is valid right now.
    assert client.get("/api/auth/me").status_code == 200

    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    # The session no longer authenticates.
    assert client.get("/api/auth/me").status_code == 401


# --------------------------------------------------------------------------- #
# Password change
# --------------------------------------------------------------------------- #
def test_password_change_wrong_old_password_is_400(client, seed_user, login):
    """A wrong current password -> 400 invalid_old_password (CSRF satisfied by the
    login fixture's header, so this exercises the credential check, not the gate)."""
    seed_user("alice")
    login("alice")
    r = client.post(
        "/api/auth/password/change",
        json={"old_password": "not-the-password", "new_password": "brand-new-pass-123"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_old_password"


def test_password_change_rotates_session(client, seed_user, login):
    """Correct old password -> 200 {"ok": true}; the PRE-change session is revoked
    (revoke_all_for_user bumps the epoch) so the old sid no longer authenticates,
    while the freshly-issued rotated session does."""
    seed_user("alice")
    login("alice")
    old_sid = client.cookies.get("sid")
    assert old_sid

    new_password = "brand-new-pass-123"
    r = client.post(
        "/api/auth/password/change",
        json={"old_password": PASSWORD, "new_password": new_password},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    # The rotated session (now in the client jar) still works.
    assert client.get("/api/auth/me").status_code == 200

    # The OLD sid was revoked: an explicit request carrying only it is rejected.
    new_sid = client.cookies.get("sid")
    assert new_sid and new_sid != old_sid
    r_old = client.get("/api/auth/me", cookies={"sid": old_sid})
    assert r_old.status_code == 401


# --------------------------------------------------------------------------- #
# Password reset-request (anti-enumeration)
# --------------------------------------------------------------------------- #
def test_reset_request_identical_body_for_known_and_unknown(client, seed_user):
    """The response body MUST be identical whether the account exists or not, so the
    endpoint is not an account-existence oracle. Both return 200 with the same body."""
    seed_user("alice")
    r_known = client.post("/api/auth/password/reset-request", json={"user_id": "alice"})
    r_unknown = client.post(
        "/api/auth/password/reset-request", json={"user_id": "ghost-who-does-not-exist"}
    )
    assert r_known.status_code == 200
    assert r_unknown.status_code == 200
    assert r_known.json() == r_unknown.json()
    assert r_known.json()["ok"] is True
    assert "message" in r_known.json()


# --------------------------------------------------------------------------- #
# Password reset-confirm
# --------------------------------------------------------------------------- #
def test_reset_confirm_bad_token_is_400(client, seed_user):
    """An unknown/garbage reset token -> 400 invalid_token (no enumeration of whether
    a token exists; reset email delivery is a STUB so we never have a real token)."""
    seed_user("alice")
    r = client.post(
        "/api/auth/password/reset-confirm",
        json={"token": "this-token-was-never-issued", "new_password": "brand-new-123"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_token"


# --------------------------------------------------------------------------- #
# Per-IP throttle (login)
# --------------------------------------------------------------------------- #
def _tight_login_ip_cap(monkeypatch, limit=3, window_s=300):
    """Lower auth_ip_max_attempts so a handful of requests can exercise the throttle without
    a slow test. MAX_FAILED_LOGINS (per-user) is left at its default 5, above `limit`, so the
    IP throttle trips first in these tests."""
    from app.config import settings

    monkeypatch.setattr(settings, "auth_ip_max_attempts", limit, raising=False)
    monkeypatch.setattr(settings, "auth_ip_window_s", window_s, raising=False)


def test_login_ip_throttle_under_limit_passes(client, seed_user, login, monkeypatch):
    """Fewer failed attempts than the per-IP cap: every one still reaches the normal
    per-user path (401 invalid_credentials), never a 429."""
    _tight_login_ip_cap(monkeypatch, limit=3)
    seed_user("alice")
    for _ in range(2):
        r = login("alice", password="nope")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "invalid_credentials"


def test_login_ip_throttle_over_limit_is_429(client, seed_user, login, monkeypatch):
    """Once the per-IP cap of failed attempts is hit, the NEXT request (from the same IP,
    against ANY user_id) is rejected with 429 ip_rate_limited instead of reaching the
    per-user credential check at all."""
    _tight_login_ip_cap(monkeypatch, limit=3)
    seed_user("alice")
    for _ in range(3):
        r = login("alice", password="nope")
        assert r.status_code == 401
    r = login("alice", password="nope")
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "ip_rate_limited"
    assert "retry_after_s" in r.json()["error"]["details"]
    assert "Retry-After" in r.headers


def test_login_ip_throttle_over_limit_blocks_other_user_ids_too(
    client, seed_user, login, monkeypatch
):
    """The cap is keyed by IP, not by user_id: after tripping it against 'alice', a login
    attempt for a DIFFERENT (even unknown) user_id from the same IP is also 429'd — this is
    the credential-stuffing mitigation (spraying guesses across many accounts from one IP)."""
    _tight_login_ip_cap(monkeypatch, limit=3)
    seed_user("alice")
    for _ in range(3):
        login("alice", password="nope")
    r = login("ghost-who-does-not-exist", password="nope")
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "ip_rate_limited"


def test_login_ip_throttle_counts_only_failed_attempts(
    client, seed_user, login, monkeypatch
):
    """A SUCCESSFUL login never burns per-IP budget: interleaving successes with failures
    only advances the counter on the failures, so the cap is reached only after `limit`
    actual failures regardless of how many successful logins happened in between."""
    _tight_login_ip_cap(monkeypatch, limit=3)
    seed_user("alice")
    for _ in range(10):
        r = login("alice", password=PASSWORD)
        assert r.status_code == 200
    # Budget is untouched by the 10 successes above: 3 failures are still required to trip it.
    for _ in range(3):
        r = login("alice", password="nope")
        assert r.status_code == 401
    r = login("alice", password="nope")
    assert r.status_code == 429


def test_login_ip_throttle_runs_before_per_user_lockout(
    client, seed_user, login, monkeypatch, session_factory
):
    """The IP throttle check happens BEFORE the per-user lookup, so once it trips, further
    requests against a specific victim account do NOT increment that account's
    failed_login_count / drive it toward the (separate) per-user lockout — an attacker who
    is IP-throttled can no longer use up the victim's lockout budget."""
    from app.auth.models import UserAccount

    def _failed_login_count(user_id):
        # A FRESH session each time (not the `db` fixture, which would keep serving a
        # cached identity-mapped instance) so this actually re-reads what the app committed.
        s = session_factory()
        try:
            return s.get(UserAccount, user_id).failed_login_count
        finally:
            s.close()

    _tight_login_ip_cap(monkeypatch, limit=1)
    user = seed_user("alice")
    r = login("alice", password="nope")
    assert r.status_code == 401  # the one attempt allowed by the IP cap
    fails_after_first = _failed_login_count(user.id)
    assert fails_after_first == 1

    # Now IP-throttled: further attempts against alice must NOT touch her per-user counter.
    for _ in range(5):
        r = login("alice", password="nope")
        assert r.status_code == 429
    assert _failed_login_count(user.id) == fails_after_first  # unchanged


def test_login_ip_throttle_disabled_switch_bypasses(
    client, seed_user, login, monkeypatch
):
    """auth_ip_throttle_enabled = False fully bypasses the per-IP cap: an attempt count that
    would otherwise 429 instead keeps reaching the normal per-user path. Stays at 4 attempts
    (below MAX_FAILED_LOGINS = 5) so the per-user lockout doesn't confound the assertion."""
    from app.config import settings

    _tight_login_ip_cap(monkeypatch, limit=2)
    monkeypatch.setattr(settings, "auth_ip_throttle_enabled", False, raising=False)
    seed_user("alice")
    for _ in range(4):
        r = login("alice", password="nope")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "invalid_credentials"


# --------------------------------------------------------------------------- #
# Per-IP throttle (reset-request)
# --------------------------------------------------------------------------- #
def test_reset_request_ip_throttle_under_limit_passes(client, seed_user, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "auth_reset_ip_max", 3, raising=False)
    seed_user("alice")
    for _ in range(2):
        r = client.post("/api/auth/password/reset-request", json={"user_id": "alice"})
        assert r.status_code == 200


def test_reset_request_ip_throttle_over_limit_is_429(client, seed_user, monkeypatch):
    """The reset-request cap counts EVERY call (not just ones for existing users) — this
    keeps the throttle from becoming a new account-existence oracle."""
    from app.config import settings

    monkeypatch.setattr(settings, "auth_reset_ip_max", 3, raising=False)
    seed_user("alice")
    for _ in range(3):
        r = client.post("/api/auth/password/reset-request", json={"user_id": "alice"})
        assert r.status_code == 200
    r = client.post(
        "/api/auth/password/reset-request", json={"user_id": "ghost-who-does-not-exist"}
    )
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "ip_rate_limited"


def test_reset_request_ip_throttle_disabled_switch_bypasses(
    client, seed_user, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "auth_reset_ip_max", 2, raising=False)
    monkeypatch.setattr(settings, "auth_ip_throttle_enabled", False, raising=False)
    seed_user("alice")
    for _ in range(5):
        r = client.post("/api/auth/password/reset-request", json={"user_id": "alice"})
        assert r.status_code == 200
