"""Admin shell tests (P5 wave B) — auth gates, the hardening headers, and the CSP-clean shell.

Mount the REAL app via the shared ``client`` fixture (so the AdminSecurityHeadersMiddleware, the
auth dependencies and the login-redirect handler are all exercised) against a throwaway SQLite DB.
"""

from __future__ import annotations

import re

from app.auth import admin_ip as admin_ip_mod

_CSP = "Content-Security-Policy"


def assert_admin_headers(resp) -> None:
    """Every /admin response carries the strict hardening set (PLAN §6)."""
    csp = resp.headers.get(_CSP, "")
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "'unsafe-inline'" not in csp  # no inline script/style ever
    assert "frame-ancestors 'none'" in csp
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Referrer-Policy") == "no-referrer"
    assert resp.headers.get("Cache-Control") == "no-store"


def assert_no_inline_scripts_or_styles(html: str) -> None:
    """CSP forbids inline script/style — enforce it at the template level (mirror of /f)."""
    for body in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S):
        assert body.strip() == "", "inline <script> body found (CSP-forbidden)"
    assert "<style" not in html, "inline <style> block found (CSP-forbidden)"
    assert 'style="' not in html, "inline style attribute found (CSP-forbidden)"
    assert not re.search(r"\son[a-z]+=", html), (
        "inline event handler found (CSP-forbidden)"
    )


# --------------------------------------------------------------------------- #
# Login page (anonymous) + headers on every response
# --------------------------------------------------------------------------- #
def test_login_page_renders_anonymous(client):
    resp = client.get("/admin/login")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert_admin_headers(resp)
    assert 'id="adm-login-form"' in resp.text
    assert_no_inline_scripts_or_styles(resp.text)


def test_headers_present_on_json_error_response(client):
    # An anonymous JSON endpoint 401 still carries the /admin hardening headers.
    resp = client.get("/admin/studio/whatever/state")
    assert resp.status_code == 401
    assert_admin_headers(resp)


# --------------------------------------------------------------------------- #
# Page auth: anonymous → login redirect, non-admin → 403
# --------------------------------------------------------------------------- #
def test_home_anonymous_redirects_to_login(client):
    resp = client.get("/admin/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/login")
    assert "next=" in resp.headers["location"]
    assert_admin_headers(resp)


def test_templates_anonymous_redirects_to_login(client):
    resp = client.get("/admin/templates", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/login")


def test_studio_page_anonymous_redirects(client):
    resp = client.get("/admin/studio/anything", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/login")


def test_home_as_reviewer_is_403(client, seed_user, login):
    seed_user("rev", role="reviewer")
    assert login("rev").status_code == 200
    resp = client.get("/admin/", follow_redirects=False)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert_admin_headers(resp)


def test_admin_must_change_password_is_redirected_from_pages(client, seed_user, login):
    # A signed-in admin who must still change their password is bounced to the login page (where the
    # change form is offered) rather than into the console.
    seed_user("newadmin", role="admin", must_change_password=True)
    assert login("newadmin").status_code == 200
    resp = client.get("/admin/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/login")


def test_admin_must_change_password_json_endpoint_is_403(client, seed_user, login):
    seed_user("newadmin", role="admin", must_change_password=True)
    assert login("newadmin").status_code == 200
    resp = client.get("/admin/studio/x/state")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "password_change_required"


def test_access_page_as_admin_renders(client, seed_user, login):
    # Regression: the access.html template must resolve (it lives at the top-level templates dir,
    # not under admin/) and the page must render for an admin — a misplacement 500s otherwise.
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    resp = client.get("/admin/access")
    assert resp.status_code == 200
    assert_admin_headers(resp)


def test_home_as_admin_renders(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    resp = client.get("/admin/")
    assert resp.status_code == 200
    assert_admin_headers(resp)
    # Area cards + capability summary + full nav (Templates / Tokens / Forms / Capabilities).
    assert "/admin/templates" in resp.text
    assert "/admin/tokens" in resp.text
    assert "/admin/forms" in resp.text
    assert "Integration health" in resp.text
    assert 'name="csrf-token"' in resp.text
    assert 'id="adm-logout"' in resp.text
    assert_no_inline_scripts_or_styles(resp.text)


# --------------------------------------------------------------------------- #
# Admin-IP allowlist deny (PLAN §6)
# --------------------------------------------------------------------------- #
def test_admin_ip_allowlist_denies(client, seed_user, login, monkeypatch):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    # A configured allowlist that the TestClient host (not a valid IP) never matches → fail closed.
    monkeypatch.setattr(
        admin_ip_mod, "configured_allowlist", lambda cfg=None: ("10.0.0.0/8",)
    )
    resp = client.get("/admin/", follow_redirects=False)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "admin_ip_forbidden"


def test_admin_ip_json_endpoint_denies(client, seed_user, login, monkeypatch):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    monkeypatch.setattr(
        admin_ip_mod, "configured_allowlist", lambda cfg=None: ("10.0.0.0/8",)
    )
    resp = client.get("/admin/studio/x/state")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "admin_ip_forbidden"


# --------------------------------------------------------------------------- #
# JSON endpoints: anonymous → 401 envelope (CSRF never preempts auth)
# --------------------------------------------------------------------------- #
def test_studio_tokenize_anonymous_is_401(client):
    resp = client.post(
        "/admin/studio/x/tokenize",
        json={
            "locator": "body/p:0",
            "start": 0,
            "end": 3,
            "token": "counterparty_name",
            "view_hash": "deadbeef",
        },
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthenticated"


def test_studio_publish_anonymous_is_401(client):
    resp = client.post("/admin/studio/x/publish", json={})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthenticated"


# --------------------------------------------------------------------------- #
# CSRF: a cookie-authed write without the double-submit header is refused
# --------------------------------------------------------------------------- #
def test_studio_write_without_csrf_header_is_403(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    # Drop the csrf header the login fixture set, keeping the session cookie → cookie-authed, no header.
    client.headers.pop("x-csrf-token", None)
    resp = client.post("/admin/studio/x/publish", json={})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"
