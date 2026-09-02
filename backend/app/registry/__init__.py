"""Token registry + drift management (PLAN §3.7 — the data + engine layer; UI is P5 wave B).

The template/token/form triangle managed as one system. Three pieces, built over the ported
``models_v2.Token`` / ``TokenTemplate`` / ``Template`` / ``TemplateVersion`` / ``DocumentBlob`` tables
PLUS one additive companion table (``token_registry_meta``, migration ``0006_token_registry``):

- :mod:`app.registry.models` — ``TokenMeta``: the user-managed metadata (label, help, data_type, party,
  fallback) that hangs off an existing ``token`` row. Additive: ported code that reads ``Token`` stays
  untouched; the registry service keeps the two in lock-step.
- :mod:`app.registry.tokens` — the CRUD service (validated snake_case names, uniqueness, meta updates,
  and a delete that first builds a full **usage report** — every template version whose .docx contains
  ``{{name}}`` + every form block bound to it — and requires ``force=True`` to proceed).
- :mod:`app.registry.drift` — drift events (token created/deleted; template published with a changed
  token set): flag affected NDA forms ``needs_update``, notify owners over the wired ``ReplyService``,
  and build/apply the one-click **sync plan** (add-field-for-new-token / unbind-or-remove-for-deleted).
- :mod:`app.registry.guard` — the generation guard ``form_bindings_complete(form, template)`` the
  generate flow consults so a required binding can never be silently unfilled.

House rules: typed, structlog, zero network (notification is fail-soft over the injected delivery),
gates fail closed / capabilities fail soft.
"""

from __future__ import annotations
