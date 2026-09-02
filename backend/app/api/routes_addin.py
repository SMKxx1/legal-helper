"""``/addin`` — serve the Word task-pane add-in SAME-ORIGIN with the API.

The add-in (``word-addin/``) is a build-free static bundle — ``taskpane.html``/``.js``/``.css``, the
Office manifest, and icon assets. Serving it from the SAME origin as the ``/api`` routes added in
later phases is deliberate: the bearer token reaches the API with no CORS setup needed.

There is no server-injected ``config.js`` here (unlike the predecessor engine): Legal Helper has no
shared API key — each user's OpenRouter key is entered once in the add-in and stored server-side
against their account (Phase 1). The add-in resolves its own API base from the origin it was served
from.

Fault-isolation: the ``word-addin/`` bundle lives at the repo root, so a backend-only checkout has no
bundle. A missing bundle must disable add-in serving, never crash app boot.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.telemetry import get_logger

log = get_logger("legal_helper.api.addin")

# word-addin/ sits at the repo root (api/ -> app/ -> backend/ -> repo), NOT under the backend package.
_REPO = Path(__file__).resolve().parents[3]
_ADDIN_DIR = _REPO / "word-addin"


def register(app: FastAPI) -> None:
    """Mount the add-in static bundle at ``/addin``. Call before the catch-all 404.

    Fault isolation: the ``word-addin/`` bundle lives OUTSIDE the ``backend/`` Docker build context in
    some deployments, so a backend-only image can have no static bundle. A missing bundle must DISABLE
    add-in serving, never crash app boot — the mount is skipped with a warning when the directory is
    absent."""
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
            note="add-in static serving disabled",
        )
