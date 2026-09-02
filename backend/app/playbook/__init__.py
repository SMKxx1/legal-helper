"""Versioned, structured playbook (Phase 1).

The playbook is the single source of truth for Amperesand's clause positions
(``playbook_build/playbook_nda_v{N}.json``). ``coverage`` derives the closed
required-clause checklist from it deterministically (improvement A) so the
coverage pass only has to *locate* clauses, never *enumerate* them.
"""
