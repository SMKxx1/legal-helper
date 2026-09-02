"""Characterization tests for the /api/admin user-management plane (routes_admin.py).

These mount the REAL app via the shared ``client`` fixture (CSRF middleware, the auth
dependencies and the error-envelope handler are all exercised) but persist to a throwaway
SQLite DB. We cover the AUTH/VALIDATION/GATE behaviour of admin user CRUD: admin-gating,
create (one-time temp password + duplicate conflict), role/status/permission PATCH, the
last-admin guard, and org-scoping. No AI/provider/network calls are made by these routes.

Contract reminders (verified against the source):
  * ``user_pk`` in the path is the UserAccount PRIMARY KEY (the uuid hex ``id``), not the login
    ``user_id`` handle. The create/list payload exposes both as ``id`` and ``user_id``.
  * Handled errors render as ``{"error": {"code", "message", "details"}}`` with the raised status.
  * The CSRF double-submit check only fires for cookie-authed state-changing requests; the
    ``login`` fixture sets the ``x-csrf-token`` header so admin mutations pass it.
"""

from __future__ import annotations

from app.schemas import DEFAULT_ORG_ID

# A second org id distinct from DEFAULT_ORG_ID, used for cross-org scoping checks.
OTHER_ORG_ID = "00000000000000000000000000000002"


# --------------------------------------------------------------------------- #
# Admin-gating: anonymous / non-admin callers are rejected with the envelope.
# --------------------------------------------------------------------------- #
def test_list_users_anonymous_is_401(client):
    resp = client.get("/api/admin/users")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "unauthenticated"
    assert isinstance(body["error"]["message"], str)
    assert isinstance(body["error"]["details"], dict)


def test_list_users_as_reviewer_is_403(client, seed_user, login):
    seed_user("rev", role="reviewer")
    assert login("rev").status_code == 200
    resp = client.get("/api/admin/users")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_list_users_as_viewer_is_403(client, seed_user, login):
    seed_user("view", role="viewer")
    assert login("view").status_code == 200
    resp = client.get("/api/admin/users")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_create_user_as_reviewer_is_403(client, seed_user, login):
    seed_user("rev", role="reviewer")
    assert login("rev").status_code == 200
    resp = client.post("/api/admin/users", json={"user_id": "newbie", "role": "viewer"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


# --------------------------------------------------------------------------- #
# Create user: success returns the temp password ONCE; duplicate -> 409.
# --------------------------------------------------------------------------- #
def test_create_user_success_returns_temp_password_once(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200

    resp = client.post(
        "/api/admin/users",
        json={"user_id": "newbie", "name": "New Bie", "role": "reviewer"},
    )
    assert resp.status_code == 201  # resource created
    body = resp.json()

    # A temp password is relayed exactly once, and the created user must change it.
    temp = body["temp_password"]
    assert isinstance(temp, str) and len(temp) >= 8

    user = body["user"]
    assert user["user_id"] == "newbie"
    assert user["name"] == "New Bie"
    assert user["role"] == "reviewer"
    assert user["status"] == "active"
    assert user["must_change_password"] is True
    assert user["permissions"] == {
        "view_all_docs": False,
        "view_all_spend": False,
        "manage_permissions": False,
    }
    # The PK is exposed as ``id`` and is what subsequent {user_pk} routes take.
    assert isinstance(user["id"], str) and user["id"]


def test_create_user_admin_supplied_temp_password_is_echoed(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    resp = client.post(
        "/api/admin/users",
        json={
            "user_id": "withpw",
            "role": "viewer",
            "temp_password": "set-by-admin-123",
        },
    )
    assert resp.status_code == 201  # resource created
    assert resp.json()["temp_password"] == "set-by-admin-123"


def test_create_user_duplicate_user_id_is_409(client, seed_user, login):
    seed_user("admin", role="admin")
    seed_user("dupe", role="viewer")  # already exists in the org
    assert login("admin").status_code == 200

    resp = client.post("/api/admin/users", json={"user_id": "dupe", "role": "viewer"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


def test_create_user_invalid_role_is_400(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    resp = client.post(
        "/api/admin/users", json={"user_id": "weird", "role": "superuser"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


def test_create_user_missing_user_id_is_422(client, seed_user, login):
    # Pydantic body validation (min_length=1 missing field) -> FastAPI 422, not our envelope.
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    resp = client.post("/api/admin/users", json={"role": "viewer"})
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# PATCH: role / status / permission updates on a target user.
# --------------------------------------------------------------------------- #
def _create_user(client, *, user_id, role="viewer"):
    resp = client.post("/api/admin/users", json={"user_id": user_id, "role": role})
    assert resp.status_code == 201  # resource created
    return resp.json()["user"]


def test_patch_user_role_and_status(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    target = _create_user(client, user_id="target", role="viewer")

    resp = client.patch(
        f"/api/admin/users/{target['id']}",
        json={"role": "reviewer", "status": "disabled"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "reviewer"
    assert body["status"] == "disabled"
    assert body["user_id"] == "target"


def test_patch_user_granular_permissions(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    target = _create_user(client, user_id="grant", role="viewer")

    resp = client.patch(
        f"/api/admin/users/{target['id']}",
        json={
            "can_view_all_docs": True,
            "can_view_all_spend": True,
            "can_manage_permissions": True,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["permissions"] == {
        "view_all_docs": True,
        "view_all_spend": True,
        "manage_permissions": True,
    }


def test_patch_user_invalid_status_is_400(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    target = _create_user(client, user_id="badstatus", role="viewer")

    resp = client.patch(
        f"/api/admin/users/{target['id']}", json={"status": "vaporized"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


def test_patch_unknown_user_is_404(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    resp = client.patch(
        "/api/admin/users/deadbeefdeadbeefdeadbeefdeadbeef", json={"role": "reviewer"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_patch_user_anonymous_is_401(client, seed_user):
    # A no-cookie state-changing admin PATCH gets the auth 401, not a CSRF 403:
    # CSRF double-submit only applies when a session cookie is present.
    u = seed_user("victim", role="viewer")
    resp = client.patch(f"/api/admin/users/{u.id}", json={"role": "reviewer"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthenticated"


# --------------------------------------------------------------------------- #
# Last-admin guard: cannot demote / disable / delete the only active admin.
# --------------------------------------------------------------------------- #
def test_cannot_demote_last_admin(client, seed_user, login):
    admin = seed_user("solo", role="admin")
    assert login("solo").status_code == 200
    resp = client.patch(f"/api/admin/users/{admin.id}", json={"role": "reviewer"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "last_admin"


def test_cannot_disable_last_admin(client, seed_user, login):
    admin = seed_user("solo", role="admin")
    assert login("solo").status_code == 200
    resp = client.patch(f"/api/admin/users/{admin.id}", json={"status": "disabled"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "last_admin"


def test_cannot_delete_last_admin(client, seed_user, login):
    admin = seed_user("solo", role="admin")
    assert login("solo").status_code == 200
    resp = client.delete(f"/api/admin/users/{admin.id}")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "last_admin"


def test_can_demote_admin_when_a_second_admin_exists(client, seed_user, login):
    # With two active admins, demoting one does not trip the floor.
    seed_user("admin1", role="admin")
    admin2 = seed_user("admin2", role="admin")
    assert login("admin1").status_code == 200
    resp = client.patch(f"/api/admin/users/{admin2.id}", json={"role": "reviewer"})
    assert resp.status_code == 200
    assert resp.json()["role"] == "reviewer"


# --------------------------------------------------------------------------- #
# Org-scoping: an admin only sees / affects users in its own org.
# --------------------------------------------------------------------------- #
def test_list_users_only_returns_own_org(client, seed_user, login):
    seed_user("admin", role="admin", org_id=DEFAULT_ORG_ID)
    seed_user("mine", role="viewer", org_id=DEFAULT_ORG_ID)
    seed_user("theirs", role="viewer", org_id=OTHER_ORG_ID)
    assert login("admin").status_code == 200

    resp = client.get("/api/admin/users")
    assert resp.status_code == 200
    user_ids = {u["user_id"] for u in resp.json()}
    assert "admin" in user_ids
    assert "mine" in user_ids
    assert "theirs" not in user_ids  # other org is invisible


def test_patch_cross_org_user_is_404(client, seed_user, login):
    # Org scoping hides another org's user behind a generic 404 (never reveal/touch it).
    seed_user("admin", role="admin", org_id=DEFAULT_ORG_ID)
    foreign = seed_user("foreign", role="viewer", org_id=OTHER_ORG_ID)
    assert login("admin").status_code == 200

    resp = client.patch(f"/api/admin/users/{foreign.id}", json={"role": "reviewer"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_delete_cross_org_user_is_404(client, seed_user, login):
    seed_user("admin", role="admin", org_id=DEFAULT_ORG_ID)
    foreign = seed_user("foreign", role="viewer", org_id=OTHER_ORG_ID)
    assert login("admin").status_code == 200

    resp = client.delete(f"/api/admin/users/{foreign.id}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- #
# Delete + reset-password (the remaining mutating routes).
# --------------------------------------------------------------------------- #
def test_delete_user_success(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    target = _create_user(client, user_id="goner", role="viewer")

    resp = client.delete(f"/api/admin/users/{target['id']}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    # It is gone from the listing.
    listing = client.get("/api/admin/users").json()
    assert "goner" not in {u["user_id"] for u in listing}


def test_reset_password_returns_new_temp_password(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    target = _create_user(client, user_id="resetme", role="viewer")

    resp = client.post(f"/api/admin/users/{target['id']}/reset-password", json={})
    assert resp.status_code == 200
    temp = resp.json()["temp_password"]
    assert isinstance(temp, str) and len(temp) >= 8

    # The user is now flagged to change their password again.
    listing = client.get("/api/admin/users").json()
    by_id = {u["id"]: u for u in listing}
    assert by_id[target["id"]]["must_change_password"] is True


def test_reset_password_unknown_user_is_404(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    resp = client.post(
        "/api/admin/users/deadbeefdeadbeefdeadbeefdeadbeef/reset-password", json={}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- #
# Read-only admin helpers under the same router.
# --------------------------------------------------------------------------- #
def test_audit_records_user_create(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200
    _create_user(client, user_id="audited", role="viewer")

    resp = client.get("/api/admin/audit")
    assert resp.status_code == 200
    events = resp.json()
    actions = {e["action"] for e in events}
    # Both the admin's own login and the create are audited in-org.
    assert "user_create" in actions
    create_evt = next(e for e in events if e["action"] == "user_create")
    assert create_evt["target"] == "audited"


def test_teams_lists_distinct_team_labels(client, seed_user, login):
    seed_user("admin", role="admin")
    seed_user("eng1", role="viewer", team="Engineering")
    seed_user("eng2", role="viewer", team="Engineering")
    seed_user("legal1", role="viewer", team="Legal")
    assert login("admin").status_code == 200

    resp = client.get("/api/admin/teams")
    assert resp.status_code == 200
    teams = resp.json()
    assert teams == sorted(set(teams))  # distinct + ordered
    assert "Engineering" in teams
    assert "Legal" in teams


def test_teams_as_viewer_is_403(client, seed_user, login):
    seed_user("view", role="viewer")
    assert login("view").status_code == 200
    resp = client.get("/api/admin/teams")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
