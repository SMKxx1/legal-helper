"""Outward integrations (PLAN §3.2, §3.9) — DocuSign now; Airtable/convert/storage in later phases.

Importing this package registers the integration ORM models on ``Base.metadata`` (via
``from . import models``), so a fresh ``create_all`` and Alembic autogenerate see ``nda_envelopes``.

Registration path (this wave): ``app.models`` — which normally carries the one-line model imports —
is owned by the FORMS agent this wave, so the DocuSign audit table is registered WITHOUT editing it:
``app.bot`` (which ``app.models`` already imports) imports this package, so any ``import app.models``
transitively loads ``app.integrations.models``. See the note in ``app/bot/__init__.py``.

The ``docusign`` module (httpx + PyJWT) is intentionally NOT imported at package load, so this
registration path stays import-light and free of the HTTP/JWT dependencies; callers import
``app.integrations.docusign`` explicitly.
"""

from __future__ import annotations

from . import models as models  # noqa: F401  (registers nda_envelopes on Base.metadata)

__all__ = ["models"]
