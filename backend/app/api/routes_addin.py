"""``/addin`` — serve the Word task-pane add-in SAME-ORIGIN with the engine (PLAN §3.1).

The add-in (``word-addin/``) is a build-free static bundle — ``taskpane.html``/``.js``/``.css``, the
Office manifests, and icon assets. Serving it from the SAME origin as ``/v1`` is deliberate: the
``SameSite=Lax`` session cookie and the ``X-API-Key`` both reach the engine with NO CORS setup (this
is the old Caddy same-origin design, re-expressed in FastAPI now that Caddy is gone).

Two pieces, registered in this order so the DYNAMIC config always wins:

* ``GET /addin/config.js`` — synthesized at REQUEST time (never read off disk) so the deployment's
  ``ENGINE_API_KEY`` is injected from ``settings``, not committed to git (the old Caddy trick). Served
  ``Cache-Control: no-store`` so a browser/proxy never caches the key. When no key is configured the
  synthesized config still parses — ``apiKey`` is left empty — so the add-in loads in its
  not-configured state (it simply sends no ``X-API-Key``) instead of failing to boot.
* ``/addin/*`` — the static bundle (``StaticFiles``), mounted AFTER the ``config.js`` route so a hit
  on ``/addin/config.js`` is answered by the synthesizer, never by the committed no-op ``config.js``
  stub that also lives in the bundle (the stub exists only for local ``dev-server.mjs`` serving).

Boot-safe: reads ``settings.engine_api_key`` per request (tests monkeypatch the ``settings``
singleton), and mounts a directory that always ships in the repo. Registered before the catch-all
404 in ``app.main`` like every other mount, so non-``/addin`` paths still hit the default-deny 404.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.telemetry import get_logger

log = get_logger("nda.api.addin")

router = APIRouter(tags=["addin"])

# word-addin/ sits at the repo root (api/ -> app/ -> backend/ -> repo), NOT under the backend package.
_REPO = Path(__file__).resolve().parents[3]
_ADDIN_DIR = _REPO / "word-addin"


def render_config_js(api_key: str) -> str:
    """The body of ``/addin/config.js``, synthesized from the engine key.

    ``apiBase`` is ``""`` (same-origin — the add-in is served alongside ``/v1``). ``apiKey`` is
    JSON-encoded (so any key value is escaped safely into the JS string) and is empty when the engine
    has no key configured — the add-in then loads unconfigured and sends no ``X-API-Key`` rather than
    breaking. The add-in reads ``window.AMP_CONFIG`` (see ``word-addin/taskpane.js``)."""
    key = (api_key or "").strip()
    note = (
        "// Synthesized per request by the engine from ENGINE_API_KEY (app.api.routes_addin).\n"
        if key
        else "// ENGINE_API_KEY is not set — the add-in loads unconfigured (no X-API-Key sent).\n"
    )
    return f'{note}window.AMP_CONFIG = {{ apiBase: "", apiKey: {json.dumps(key)} }};\n'


@router.get("/addin/config.js")
async def addin_config_js() -> Response:
    """Serve the request-time-synthesized add-in config (no-store; key from settings)."""
    body = render_config_js(getattr(settings, "engine_api_key", "") or "")
    return Response(
        content=body,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


def register(app: FastAPI) -> None:
    """Wire the add-in surface: the synthesized ``config.js`` route FIRST, then the static bundle.

    Order matters — both live in ``app.router.routes`` and Starlette returns the first match, so
    including the router before mounting ``/addin`` guarantees ``/addin/config.js`` resolves to the
    synthesizer and never to the committed stub inside the bundle. Call before the catch-all 404.

    Fault isolation (PLAN §1): the ``word-addin/`` bundle lives at the repo root, OUTSIDE the
    ``backend/`` Docker build context, so a backend-only image has no static bundle. A missing bundle
    must DISABLE add-in serving, never crash app boot — the router (incl. the synthesized config.js) is
    always wired; the static mount is skipped with a warning when the directory is absent."""
    app.include_router(router)
    if _ADDIN_DIR.is_dir():
        app.mount(
            "/addin",
            StaticFiles(directory=str(_ADDIN_DIR), html=False),
            name="addin-static",
        )
    else:
        log.warning(
            "addin.bundle_missing",
            directory=str(_ADDIN_DIR),
            note="add-in static serving disabled; config.js route still active",
        )
