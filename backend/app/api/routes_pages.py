"""``GET /api/status`` and the ``GET /`` landing page (plan §4.3) — both PUBLIC, no auth, no
secrets. This is the one surface a curious visitor (or a workshop audience) sees without signing
in: capability states, org-wide totals, and a link to the Word manifest — the same "is anything
actually configured" story the capability registry (``app.capabilities``) tells internally.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Template
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from ..capabilities import CapabilityRegistry
from ..db import get_db
from ..models import Review, User

router = APIRouter(tags=["pages"])

_APP_VERSION = "0.1.0"


def _commit() -> str:
    """A short commit SHA if the deploy environment set one (Railway sets ``RAILWAY_GIT_COMMIT_SHA``
    automatically), else ``"dev"`` — never a boot error either way."""
    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("GIT_COMMIT") or ""
    return sha[:7] if sha else "dev"


def _region() -> str:
    return (
        os.environ.get("RAILWAY_REPLICA_REGION")
        or os.environ.get("RAILWAY_REGION")
        or "local"
    )


def _totals(db: DbSession) -> dict:
    """Org-wide, all-time. Cost sums across EVERY review regardless of status — a failed review can
    still carry partial spend from calls made before it failed (``reviews_repo.fail_review``), and
    that is real money already spent."""
    users = int(db.execute(select(func.count(User.id))).scalar() or 0)
    reviews = int(db.execute(select(func.count(Review.id))).scalar() or 0)
    cost_usd = round(
        float(
            db.execute(select(func.coalesce(func.sum(Review.cost_usd), 0.0))).scalar()
            or 0.0
        ),
        2,
    )
    return {"users": users, "reviews": reviews, "cost_usd": cost_usd}


def _status_payload(request: Request, db: DbSession) -> dict:
    registry: CapabilityRegistry = request.app.state.capabilities
    capabilities = {c["name"]: c["state"] for c in registry.report()}
    started_at = getattr(request.app.state, "started_at", None)
    uptime_s = (
        round(time.monotonic() - started_at, 1) if started_at is not None else 0.0
    )
    return {
        "version": _APP_VERSION,
        "commit": _commit(),
        "uptime_s": uptime_s,
        "region": _region(),
        "capabilities": capabilities,
        "totals": _totals(db),
    }


@router.get("/api/status")
def get_status(request: Request, db: DbSession = Depends(get_db)) -> JSONResponse:
    """Public, no secrets: capability STATE only (``"enabled"``/``"disabled"``/``"unhealthy"``),
    never the config values or reasons behind it (those stay in server logs — see
    ``app.capabilities.CapabilityStatus.reason``)."""
    return JSONResponse(_status_payload(request, db))


_LANDING_TEMPLATE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Legal Helper</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: #faf7f1; color: #102027;
    font-family: "Hanken Grotesk", system-ui, -apple-system, "Segoe UI", sans-serif;
    line-height: 1.5;
  }
  header { background: #102027; color: #fff; padding: 28px 20px; border-bottom: 3px solid #b7965d; }
  h1 { margin: 0; font-size: 22px; letter-spacing: -0.01em; }
  header p { margin: 4px 0 0; color: #8da0a6; font-size: 13px; }
  main { max-width: 720px; margin: 0 auto; padding: 24px 20px 48px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin: 0 0 24px; }
  .tile {
    border: 1px solid #e3ddd3; border-radius: 8px; padding: 14px 16px; background: #fff;
  }
  .tile .num { font-size: 24px; font-weight: 700; display: block; }
  .tile .label {
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.1em; color: #6a6a6a; margin-top: 2px;
  }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.1em; color: #6a6a6a; margin: 24px 0 10px; }
  .caps { border: 1px solid #e3ddd3; border-radius: 8px; overflow: hidden; background: #fff; }
  .cap-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 14px; border-bottom: 1px solid #e3ddd3; font-size: 13px;
  }
  .cap-row:last-child { border-bottom: none; }
  .pill {
    font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em;
    padding: 3px 9px; border-radius: 999px;
  }
  .pill.enabled { background: #e3f3ea; color: #1f7a4d; }
  .pill.disabled { background: #efeae2; color: #6a6a6a; }
  .pill.unhealthy { background: #fbedea; color: #b5331f; }
  .meta { font-size: 12px; color: #6a6a6a; margin-top: 4px; }
  a.btn {
    display: inline-block; margin-top: 24px; background: #b7965d; color: #fff; text-decoration: none;
    font-weight: 700; padding: 11px 18px; border-radius: 6px; font-size: 13px;
  }
  a.btn:hover { background: #c9a96e; }
</style>
</head>
<body>
<header>
  <h1>Legal Helper</h1>
  <p>Document review for Word — status &amp; usage</p>
</header>
<main>
  <div class="grid">
    <div class="tile"><span class="num">{{ status.totals.users }}</span><span class="label">Users</span></div>
    <div class="tile"><span class="num">{{ status.totals.reviews }}</span><span class="label">Reviews</span></div>
    <div class="tile"><span class="num">${{ "%.2f"|format(status.totals.cost_usd) }}</span><span class="label">Total spend</span></div>
  </div>

  <h2>Capabilities</h2>
  <div class="caps">
    {% for name, state in status.capabilities.items() %}
    <div class="cap-row">
      <span>{{ name }}</span>
      <span class="pill {{ state }}">{{ state }}</span>
    </div>
    {% endfor %}
  </div>
  <p class="meta">v{{ status.version }} &middot; {{ status.commit }} &middot; {{ status.region }} &middot; up {{ status.uptime_s }}s</p>

  <a class="btn" href="/manifest.xml">Download manifest</a>
</main>
</body>
</html>
"""
)


@router.get("/", response_class=HTMLResponse)
def landing_page(request: Request, db: DbSession = Depends(get_db)) -> HTMLResponse:
    status = _status_payload(request, db)
    return HTMLResponse(
        _LANDING_TEMPLATE.render(status=status, now=datetime.now(UTC).isoformat())
    )
