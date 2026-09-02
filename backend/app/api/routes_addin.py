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

import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.telemetry import get_logger

log = get_logger("legal_helper.api.addin")

# word-addin/ sits at the repo root (api/ -> app/ -> backend/ -> repo), NOT under the backend package.
_REPO = Path(__file__).resolve().parents[3]
_ADDIN_DIR = _REPO / "word-addin"

# The dev manifest is the single source of truth for the manifest's SHAPE. The deployed
# /manifest.xml is that same file with the localhost dev origin rewritten to the origin the
# request arrived on, so one sideloaded file works for every deployment without a build step.
_DEV_ORIGIN = "https://localhost:3000"
_MANIFEST_TEMPLATE = _ADDIN_DIR / "manifest.dev.xml"


def _origin(request: Request) -> str:
    """The public origin this request arrived on.

    Behind Railway's proxy the app speaks plain HTTP internally, so trust ``X-Forwarded-Proto``
    when present — Office rejects any non-localhost manifest URL that is not HTTPS.
    """
    proto = (
        request.headers.get("x-forwarded-proto", request.url.scheme)
        .split(",")[0]
        .strip()
    )
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def build_manifest(template: str, origin: str, addin_id: str, version: str) -> str:
    """Rewrite the dev manifest for ``origin``. Pure function so it is trivially testable.

    Static assets live under the ``/addin`` mount; the landing page and AppDomain are the bare
    origin. Replacements run most-specific-first so the bare-origin rule cannot eat the others.
    """
    out = template.replace(f"{_DEV_ORIGIN}/assets/", f"{origin}/addin/assets/")
    out = out.replace(f"{_DEV_ORIGIN}/taskpane.html", f"{origin}/addin/taskpane.html")
    out = out.replace(f"{_DEV_ORIGIN}/", f"{origin}/")
    out = out.replace(_DEV_ORIGIN, origin)
    # Stable per-deployment GUID: Office keys the installed add-in on <Id>.
    out = re.sub(r"<Id>[^<]*</Id>", f"<Id>{addin_id}</Id>", out, count=1)
    # Office requires a 4-part version.
    parts = (version.split(".") + ["0", "0", "0", "0"])[:4]
    out = re.sub(
        r"<Version>[^<]*</Version>",
        f"<Version>{'.'.join(parts)}</Version>",
        out,
        count=1,
    )
    return out


def register(app: FastAPI) -> None:
    """Mount the add-in static bundle at ``/addin``. Call before the catch-all 404.

    Fault isolation: the ``word-addin/`` bundle lives OUTSIDE the ``backend/`` Docker build context in
    some deployments, so a backend-only image can have no static bundle. A missing bundle must DISABLE
    add-in serving, never crash app boot — the mount is skipped with a warning when the directory is
    absent."""
    if not _ADDIN_DIR.is_dir():
        log.warning(
            "addin.bundle_missing",
            directory=str(_ADDIN_DIR),
            note="add-in static serving disabled",
        )
        return

    @app.get("/manifest.xml", include_in_schema=False)
    def manifest(request: Request) -> Response:
        """The sideloadable Office manifest, pointed at THIS deployment's origin.

        Generated per request rather than committed per environment: the same deployment serves a
        working manifest whether it is reached over its Railway domain or a custom one.
        """
        settings = get_settings()
        xml = build_manifest(
            _MANIFEST_TEMPLATE.read_text(encoding="utf-8"),
            origin=_origin(request),
            addin_id=settings.addin_id,
            version="0.1.0",
        )
        return Response(
            content=xml,
            media_type="application/xml",
            headers={
                "Content-Disposition": 'attachment; filename="legal-helper-manifest.xml"'
            },
        )

    app.mount(
        "/addin",
        StaticFiles(directory=str(_ADDIN_DIR), html=False),
        name="addin-static",
    )
