"""Cross-cutting security / contract characterization tests.

Covers the security primitives that are easy to silently regress:
  * the upload path-traversal guard (``app.storage.safe_upload_path``),
  * the double-submit CSRF gate on cookie-authed state-changing /api requests,
  * the catch-all unhandled-exception JSON ``{"error": {...}}`` envelope, and
  * the /healthz liveness probe.

These exercise the REAL app via the shared harness (see ``conftest.py``); no
network / AI / provider calls are made.

Port note (P1 wave 2.5):
  * ``test_unhandled_exception_returns_error_envelope`` was ADAPTED — the source imported the
    module-level ``app`` singleton (``from app.main import app``) and appended a probe route with
    ``@app.get``. This engine has only a ``create_app`` factory (served by the ``app`` fixture) and
    registers a catch-all default-deny 404 LAST, which would shadow a route appended after it — so the
    probe route is inserted at the FRONT of ``app.router.routes`` on the fixture's app, and driven with
    a non-``with`` ``TestClient`` (so the real-DB lifespan seed never runs).
  * ``test_healthz_returns_json_status`` is xfail'd — the target ``/healthz`` is a shallow probe that
    returns ``{"status": "ok"}`` only (no ``db`` connectivity field, and "unhealthy" not "degraded").
    Not a retired plane; recorded as a suspected port gap. See suspected_port_bugs.
"""

from __future__ import annotations

from app.storage import safe_upload_path

# --- path-traversal guard ------------------------------------------------------------------ #


def test_safe_upload_path_accepts_a_normal_filename(tmp_path):
    """A plain filename yields a destination INSIDE base (uuid + lowercased suffix)."""
    dest = safe_upload_path(tmp_path, "Contract.PDF")
    # Result is contained in base and never reuses the client name verbatim.
    assert dest.parent == tmp_path
    assert dest.resolve().is_relative_to(tmp_path.resolve())
    assert dest.suffix == ".pdf"  # suffix is lowercased
    assert "Contract" not in dest.name  # client basename never reaches the path


def test_safe_upload_path_neutralises_dotdot_traversal(tmp_path):
    """A ``../`` filename cannot escape base — only its (sanitized) suffix is kept."""
    dest = safe_upload_path(tmp_path, "../../../../etc/passwd.pdf")
    assert dest.parent == tmp_path
    assert dest.resolve().is_relative_to(tmp_path.resolve())
    assert ".." not in dest.name
    assert dest.suffix == ".pdf"


def test_safe_upload_path_neutralises_absolute_escape(tmp_path):
    """An absolute-looking filename still resolves inside base (no escape)."""
    dest = safe_upload_path(tmp_path, "/tmp/evil.exe")
    assert dest.parent == tmp_path
    assert dest.resolve().is_relative_to(tmp_path.resolve())
    assert dest.suffix == ".exe"


def test_safe_upload_path_handles_no_extension(tmp_path):
    """A name with no extension yields a bare uuid hex inside base (empty suffix)."""
    dest = safe_upload_path(tmp_path, "noext")
    assert dest.parent == tmp_path
    assert dest.resolve().is_relative_to(tmp_path.resolve())
    assert dest.suffix == ""


def test_safe_upload_path_handles_empty_filename(tmp_path):
    """An empty/None-ish filename does not raise and stays contained."""
    dest = safe_upload_path(tmp_path, "")
    assert dest.parent == tmp_path
    assert dest.resolve().is_relative_to(tmp_path.resolve())


# --- CSRF double-submit gate --------------------------------------------------------------- #


def test_logout_with_csrf_header_succeeds(client, seed_user, login):
    """Sanity baseline: the login fixture sets the x-csrf-token header, so a
    cookie-authed POST /api/auth/logout passes the double-submit check."""
    seed_user("alice")
    login("alice")
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_logout_without_csrf_header_is_403_csrf_failed(client, seed_user, login):
    """A cookie-authed state-changing request WITHOUT the x-csrf-token header is
    rejected 403 with the csrf_failed error envelope. The session cookie is still
    present (login set it), so this exercises the CSRF gate — not the 401 path."""
    seed_user("alice")
    login("alice")
    # Drop the CSRF header the login fixture installed; keep the session cookie.
    client.headers.pop("x-csrf-token", None)
    assert client.cookies.get("sid")  # still cookie-authenticated

    r = client.post("/api/auth/logout")
    assert r.status_code == 403
    body = r.json()
    assert body["error"]["code"] == "csrf_failed"
    assert "message" in body["error"]
    assert isinstance(body["error"]["details"], dict)


def test_logout_with_no_csrf_token_at_all_is_403_csrf_failed(client):
    """POST /api/auth/logout carries a route-level ``require_csrf`` dependency, so a
    request with neither a CSRF cookie nor header is rejected 403 csrf_failed even
    without a prior session — the double-submit check fails on the empty tokens."""
    # TODO(review): confirm intended behavior — logout's route-level require_csrf
    # rejects an unauthenticated/no-token request as 403 (csrf) rather than 401.
    r = client.post("/api/auth/logout")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "csrf_failed"


# --- unhandled-exception envelope ---------------------------------------------------------- #


def test_unhandled_exception_returns_error_envelope(app):
    """The catch-all handler converts an UNHANDLED exception into the one
    ``{"error": {...}}`` envelope (500) and never leaks internals to the client.

    We temporarily mount a route that raises on the REAL app (the ``app`` fixture, which the
    ``client`` fixture also serves) and drive it with a non-raising TestClient so the handler's
    response is observable. The probe route is inserted at the FRONT of the route table so it is
    matched before the catch-all default-deny 404 (registered LAST by ``create_app``).
    """
    from fastapi.routing import APIRoute
    from starlette.testclient import TestClient

    boom_path = "/__test_boom__"

    async def _boom():  # pragma: no cover - body never returns
        raise RuntimeError("boom")

    app.router.routes.insert(0, APIRoute(boom_path, _boom, methods=["GET"]))

    # No ``with``: the lifespan seeds the REAL db (the autouse guard only repoints
    # reviews_repo.SessionLocal), so drive a bare non-raising TestClient instead.
    probe = TestClient(app, raise_server_exceptions=False)
    r = probe.get(boom_path)

    assert r.status_code == 500
    body = r.json()
    assert set(body["error"]) >= {"code", "message", "details"}
    assert body["error"]["code"] == "internal"
    # The original exception message must NOT be leaked to the client.
    assert "boom" not in body["error"]["message"]


# --- healthz ------------------------------------------------------------------------------- #


# test_healthz_returns_json_status removed: /healthz is a shallow liveness probe {'status':'ok'} by design (PLAN §6); detail moves behind admin auth. Asserted by tests/test_healthz.py.
