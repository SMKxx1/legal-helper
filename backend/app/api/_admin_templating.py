"""Shared Jinja env for the admin builder + token-registry pages (P5 wave B, agent 2).

The admin shell (``admin/base.html`` + login + nav + the security-headers middleware + mounting) is
owned by the studio agent and authored concurrently. This module lets agent-2's pages render against
that shell TODAY — without authoring a competing shell — by layering a tiny in-memory **stand-in**
base UNDER the real one:

    ChoiceLoader([ FileSystemLoader(app/admin/templates),   # agent-1's real admin/base.html wins
                   DictLoader({"admin/base.html": _STANDIN_BASE}) ])  # fallback until it lands

The FileSystemLoader is consulted first, so the moment ``app/admin/templates/admin/base.html`` exists
it supersedes the stand-in with zero code change here. The stand-in only defines the three documented
blocks — ``{% block title %}`` / ``{% block content %}`` / ``{% block page_js %}`` — so a child that
extends ``admin/base.html`` renders identically under either.

House rules honoured: server-rendered Jinja, autoescape ON everywhere (untrusted submission/token
values must escape), and CSP-clean output — the stand-in references only external static assets, never
an inline ``<script>``/``<style>``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from app.auth.sessions import Principal

#: agent-1 owns files under here (admin/base.html, login, nav); agent-2 adds builder/ + tokens/.
ADMIN_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "admin" / "templates"

#: Minimal stand-in for the shared shell, used ONLY until agent-1's real admin/base.html is present.
#: Defines exactly the documented block contract so agent-2 pages render either way. No inline JS/CSS.
_STANDIN_BASE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Admin{% endblock %} · NDA Assistant</title>
  <link rel="stylesheet" href="/admin/static/builder/builder.css">
  <link rel="stylesheet" href="/admin/static/tokens/tokens.css">
</head>
<body class="adm">
  <header class="adm-topbar">
    <span class="adm-brand">NDA Assistant</span>
    <nav class="adm-nav">
      <a href="/admin/forms" class="{% if active_nav == 'forms' %}is-active{% endif %}">Forms</a>
      <a href="/admin/tokens" class="{% if active_nav == 'tokens' %}is-active{% endif %}">Tokens</a>
    </nav>
    {% if user_id %}<span class="adm-user">{{ user_id }}</span>{% endif %}
  </header>
  <main class="adm-main">
    {% block content %}{% endblock %}
  </main>
  {% block page_js %}{% endblock %}
</body>
</html>
"""


def _build_templates() -> Jinja2Templates:
    ADMIN_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    env = Environment(
        loader=ChoiceLoader(
            [
                FileSystemLoader(str(ADMIN_TEMPLATES_DIR)),
                DictLoader({"admin/base.html": _STANDIN_BASE}),
            ]
        ),
        autoescape=True,  # every admin page renders untrusted values; escape unconditionally
        auto_reload=False,
    )
    return Jinja2Templates(env=env)


#: The shared admin Jinja templates instance (import this from the builder + token routes).
admin_templates: Jinja2Templates = _build_templates()


def page_context(
    request: Request,
    principal: Principal | None,
    active_nav: str,
    **extra: Any,
) -> dict[str, Any]:
    """Assemble the base render context every admin page shares (nav highlight + signed-in user id),
    merged with page-specific ``extra``. ``request`` is required by Starlette's TemplateResponse."""
    ctx: dict[str, Any] = {
        "active_nav": active_nav,
        "user_id": (principal.user_id if principal else None),
    }
    ctx.update(extra)
    return ctx
