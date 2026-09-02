"""Template studio document-surgery core (PLAN §3.7) — the highlight→click tokenizer.

The studio edits real legal .docx **draft** template versions in place, so every module here is
document-integrity-critical: a refusal is always preferable to a corrupt NDA.

Package layout:

- ``errors``       — the typed refusal taxonomy (all ``EngineError`` subclasses, so routes can let
  them propagate straight into the standard error envelope).
- ``docview``      — ``extract_view``: the stable, addressable read-only text model of a .docx
  (locator + normalized run-concatenated paragraph text + content hash for staleness).
- ``tokenize_ops`` — ``apply_tokenize`` / ``undo_tokenize``: run-aware span→``{{token}}``
  replacement, the exact inverse of the generation filler
  (``app.support_task.generator.fill_docx``), plus its byte-faithful inverse for undo.
- ``models``       — the ``studio_ops`` operations-trail table (registered on ``Base.metadata`` via
  ``app.models``; created by Alembic ``0007_studio_ops``).
- ``oplog``        — the per-draft-version operations log: atomic apply/undo/redo with
  content-hash optimistic concurrency and standard editor redo-tail truncation.
- ``findmap``      — the find-and-map assistant: typed-placeholder detection (``[COMPANY NAME]``,
  ``<Company>``, ``____`` …) with fuzzy token suggestions, and batch mapping.
- ``checklist``    — the live checklist: token scan (same traversal/regex family as the
  generation-side unfilled-token guard) → found / missing-required / unknown-with-suggestion.

Token *validity* (is this a registered token?) is deliberately NOT checked here — that is the
caller's job against the token registry; this layer takes a token *name string* and only enforces
the structural shape that keeps the document well-formed.
"""
