"""Word add-in serving (P6 — PLAN §3.1): the ``/addin`` surface mounted same-origin with ``/v1``.

Exercised through the REAL app (``create_app``) so the route/mount ORDER (the synthesized
``config.js`` must beat the static stub of the same name) and the catch-all 404 interaction are all
real. Asserts:

* the static bundle serves (``taskpane.html`` / ``taskpane.js`` -> 200);
* ``GET /addin/config.js`` is SYNTHESIZED per request — it injects ``ENGINE_API_KEY`` when set, and
  leaves ``apiKey`` empty (the not-configured state, still valid JS) when unset — never the committed
  stub, and always ``Cache-Control: no-store`` so the key is never cached;
* mounting ``/addin`` does not disturb the default-deny 404: a non-``/addin`` path still returns the
  ``{"error": "not_found"}`` envelope.
"""

from __future__ import annotations

import pytest

from app.api.routes_addin import render_config_js


@pytest.fixture(autouse=True)
def _clear_engine_key(monkeypatch):
    """Default every test to the UNSET-key state; the injection test opts into a key explicitly.
    Monkeypatches the process-wide ``settings`` singleton the route reads per request."""
    from app.config import settings

    monkeypatch.setattr(settings, "engine_api_key", "")
    return settings


# --------------------------------------------------------------------------- #
# Static bundle
# --------------------------------------------------------------------------- #
def test_addin_static_taskpane_served(client):
    """The add-in HTML entrypoint is served from the same origin as the engine."""
    resp = client.get("/addin/taskpane.html")
    assert resp.status_code == 200, resp.text
    assert "text/html" in resp.headers["content-type"]
    assert "Amperesand" in resp.text


def test_addin_static_taskpane_js_served(client):
    resp = client.get("/addin/taskpane.js")
    assert resp.status_code == 200, resp.text
    # the ported add-in logic, not an error page
    assert "AMP_CONFIG" in resp.text


# --------------------------------------------------------------------------- #
# config.js — synthesized both states, no-store
# --------------------------------------------------------------------------- #
def test_config_js_injects_engine_key_when_set(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "engine_api_key", "svc-secret-key-123")
    resp = client.get("/addin/config.js")

    assert resp.status_code == 200, resp.text
    assert "javascript" in resp.headers["content-type"]
    assert resp.headers["cache-control"] == "no-store"
    body = resp.text
    assert "svc-secret-key-123" in body  # the key is injected
    assert 'apiBase: ""' in body  # same-origin
    assert "window.AMP_CONFIG" in body


def test_config_js_serves_unconfigured_state_when_key_unset(client):
    """No key configured -> valid JS with an EMPTY apiKey (the add-in loads unconfigured and sends
    no X-API-Key) rather than a broken/absent config."""
    resp = client.get("/addin/config.js")

    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    body = resp.text
    assert 'apiKey: ""' in body
    assert "window.AMP_CONFIG" in body
    # nothing that looks like a leaked key
    assert "svc-secret" not in body


def test_config_js_beats_the_static_stub(client, monkeypatch):
    """/addin/config.js resolves to the synthesizer, not the committed no-op stub in the bundle."""
    from app.config import settings

    monkeypatch.setattr(settings, "engine_api_key", "beats-the-stub")
    resp = client.get("/addin/config.js")
    assert resp.status_code == 200
    assert (
        resp.headers["cache-control"] == "no-store"
    )  # the stub is static -> no no-store header
    assert "beats-the-stub" in resp.text


def test_config_js_is_valid_javascript_both_states():
    """The synthesized body is well-formed JS (a leading comment + one AMP_CONFIG assignment) in
    both states, and the key is JSON-escaped so an exotic key value can't break the string."""
    unset = render_config_js("")
    assert (
        unset.strip().splitlines()[-1]
        == 'window.AMP_CONFIG = { apiBase: "", apiKey: "" };'
    )
    # a key with characters that MUST be escaped to stay inside the JS string literal
    injected = render_config_js('ab"c\\d')
    assert 'apiKey: "ab\\"c\\\\d"' in injected


# --------------------------------------------------------------------------- #
# The mount does not disturb the default-deny 404
# --------------------------------------------------------------------------- #
def test_non_addin_path_still_default_deny_404(client):
    resp = client.get("/definitely/not/a/route")
    assert resp.status_code == 404
    assert resp.json() == {"error": "not_found", "path": "/definitely/not/a/route"}


def test_addin_unknown_asset_is_404(client):
    """A missing file UNDER /addin is a 404 (handled by the static mount) — not a 200 stub and not a
    traversal escape."""
    assert client.get("/addin/does-not-exist.js").status_code == 404


def test_register_survives_missing_addin_bundle(monkeypatch):
    """Fault isolation (PLAN §1): the word-addin/ bundle lives outside the backend/ Docker context, so
    a backend-only image has no bundle. register() must SKIP the static mount and keep booting — a
    missing optional bundle disables add-in serving, it never crashes the app (this exact RuntimeError
    took down the first Azure dev boot). config.js stays wired regardless."""
    from pathlib import Path

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import routes_addin

    monkeypatch.setattr(routes_addin, "_ADDIN_DIR", Path("/does/not/exist/word-addin"))
    app = FastAPI()
    routes_addin.register(app)  # must not raise
    c = TestClient(app)
    # config.js still served (router wired even without the static bundle)...
    assert c.get("/addin/config.js").status_code == 200
    # ...but the static bundle is absent, so a bundle asset is simply not found (no crash).
    assert c.get("/addin/taskpane.html").status_code == 404
