"""Word add-in serving: the ``/addin`` surface mounted same-origin with the API.

Exercised through the REAL app (``create_app``) so the mount order and the catch-all 404
interaction are both real. There is no server-injected ``config.js`` in this engine (unlike the
predecessor) — no shared API key exists to inject; the add-in resolves its own origin client-side.
"""

from __future__ import annotations


def test_addin_static_taskpane_served(client):
    """The add-in HTML entrypoint is served from the same origin as the API."""
    resp = client.get("/addin/taskpane.html")
    assert resp.status_code == 200, resp.text
    assert "text/html" in resp.headers["content-type"]
    assert "Legal Helper" in resp.text


def test_addin_static_taskpane_js_served(client):
    resp = client.get("/addin/taskpane.js")
    assert resp.status_code == 200, resp.text
    # the ported add-in logic, not an error page
    assert "runReview" in resp.text


def test_manifest_is_served_and_points_at_the_request_origin(client):
    """The manifest must advertise THIS deployment's origin, never the dev localhost origin.

    Office fetches taskpane.html from whatever the manifest says; a manifest still pointing at
    https://localhost:3000 installs cleanly and then shows a blank pane on every machine but the
    developer's.
    """
    resp = client.get("/manifest.xml")
    assert resp.status_code == 200, resp.text
    assert "xml" in resp.headers["content-type"]
    body = resp.text
    assert "https://localhost:3000" not in body
    assert "testserver/addin/taskpane.html" in body
    assert "testserver/addin/assets/icon-32.png" in body


def test_manifest_uses_configured_addin_id_and_four_part_version():
    """<Id> is the key Office installs against, so it comes from config, not the dev template."""
    from app.api.routes_addin import build_manifest

    template = (
        '<Id>dev-guid</Id><Version>1.0.0.0</Version><a href="https://localhost:3000/"/>'
    )
    out = build_manifest(
        template, "https://x.example", addin_id="real-guid", version="0.1.0"
    )
    assert "<Id>real-guid</Id>" in out
    assert "<Version>0.1.0.0</Version>" in out
    assert "https://x.example/" in out


def test_non_addin_path_still_default_deny_404(client):
    resp = client.get("/definitely/not/a/route")
    assert resp.status_code == 404
    assert resp.json() == {"error": "not_found", "path": "/definitely/not/a/route"}


def test_addin_unknown_asset_is_404(client):
    """A missing file UNDER /addin is a 404 (handled by the static mount) — not a 200 stub and not a
    traversal escape."""
    assert client.get("/addin/does-not-exist.js").status_code == 404


def test_register_survives_missing_addin_bundle(monkeypatch):
    """Fault isolation: the word-addin/ bundle lives outside the backend/ Docker build context in some
    deployments, so a backend-only image can have no bundle. register() must SKIP the static mount and
    keep booting — a missing optional bundle disables add-in serving, it never crashes the app."""
    from pathlib import Path

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import routes_addin

    monkeypatch.setattr(routes_addin, "_ADDIN_DIR", Path("/does/not/exist/word-addin"))
    app = FastAPI()
    routes_addin.register(app)  # must not raise
    c = TestClient(app)
    assert c.get("/addin/taskpane.html").status_code == 404
