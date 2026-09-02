"""The admin capability report endpoint (PLAN §6, §2 decision 4) — GET /api/admin/capabilities.

The admin-gated counterpart of the shallow public ``/healthz``: it returns the boot-time capability
report (per-integration enabled/disabled/unhealthy) plus the app version + env, behind ``require_admin``
so config state never leaks anonymously. Mounts the REAL app via the shared ``client`` fixture (auth
dependencies + the error envelope are exercised) but persists to a throwaway SQLite DB; no network.
"""

from __future__ import annotations


# --------------------------------------------------------------------------- #
# Auth gate: anonymous / non-admin are rejected with the standard envelope.
# --------------------------------------------------------------------------- #
def test_capabilities_anonymous_is_401(client):
    resp = client.get("/api/admin/capabilities")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthenticated"


def test_capabilities_as_reviewer_is_403(client, seed_user, login):
    seed_user("rev", role="reviewer")
    assert login("rev").status_code == 200
    resp = client.get("/api/admin/capabilities")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_capabilities_as_viewer_is_403(client, seed_user, login):
    seed_user("view", role="viewer")
    assert login("view").status_code == 200
    resp = client.get("/api/admin/capabilities")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


# --------------------------------------------------------------------------- #
# Shape: an admin gets the version/env + the per-capability report.
# --------------------------------------------------------------------------- #
def test_capabilities_admin_returns_report_shape(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200

    resp = client.get("/api/admin/capabilities")
    assert resp.status_code == 200
    body = resp.json()

    # app block: version + env are present and stringy.
    assert "app" in body
    assert isinstance(body["app"]["version"], str) and body["app"]["version"]
    assert isinstance(body["app"]["env"], str)

    # capabilities: a non-empty list of the boot-time report rows.
    caps = body["capabilities"]
    assert isinstance(caps, list) and caps
    by_name = {c["name"]: c for c in caps}
    # Known integrations from build_registry are present.
    for name in ("slack", "docusign", "google_drive", "airtable", "tally"):
        assert name in by_name
    # Each row carries exactly the report contract keys — states only, never secret values.
    for c in caps:
        assert set(c) == {"name", "state", "reason", "summary", "critical"}
        assert c["state"] in {"enabled", "disabled", "unhealthy"}
        assert isinstance(c["critical"], bool)


def test_capabilities_disabled_without_config(client, seed_user, login):
    """With no provider config in the test env, soft capabilities report ``disabled`` (not leaked
    publicly) — the report exists precisely to show that behind auth."""
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    caps = {
        c["name"]: c
        for c in client.get("/api/admin/capabilities").json()["capabilities"]
    }
    # DocuSign has required config; absent it, the capability is disabled with a config-naming reason.
    assert caps["docusign"]["state"] == "disabled"
    assert "missing config" in caps["docusign"]["reason"]
