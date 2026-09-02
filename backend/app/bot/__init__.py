"""The in-process bot core (PLAN §3.2) — Slack + email intake, guards, routing, intents, replies.

This package replaces the retired n8n "NDA Assistant" workflow stack with ordinary transactional
Python. The FOUNDATION layer lives here: the normalized :class:`~app.bot.envelope.Envelope` every
intake path produces, and the persistence primitives in :mod:`app.bot.models` (dedup inbox,
allowlist, pending approvals, correlation state). The four channel/router/worker builders compose on
top of these.

Only the envelope types are re-exported at the package root — they are the cross-cutting contract.
The ORM models are imported from :mod:`app.models` (which registers them on ``Base.metadata``), not
here, to keep this ``__init__`` import-light and free of a SQLAlchemy dependency.
"""

from __future__ import annotations

# Cross-wave metadata registration (PLAN §3.9 — DocuSign agent). Importing ``app.integrations`` here
# registers the ``nda_envelopes`` audit ORM model on ``Base.metadata``. ``app.models`` (which imports
# ``app.bot``) is owned by the forms agent this wave, so the DocuSign table cannot be registered by an
# ``app.models`` line; wiring it through this import means every ``import app.models`` — the create_all
# path used by the seed, the tests, and Alembic autogenerate — also loads it. Only the light
# ``app.integrations.models`` is pulled in (no httpx/PyJWT); see ``app/integrations/__init__.py``.
from .. import integrations as _integrations  # noqa: F401
from .envelope import AttachmentRef, Channel, Envelope

__all__ = ["AttachmentRef", "Channel", "Envelope"]
