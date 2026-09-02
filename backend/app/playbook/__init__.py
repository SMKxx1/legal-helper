"""Versioned, structured playbook.

The playbook is the single source of truth for the reviewer's clause positions. ``coverage``
derives the closed required-clause checklist from it deterministically so the coverage pass only
has to *locate* clauses, never *enumerate* them. Phase 2 replaces the NDA-specific playbook here
with the generic ``playbook/legal_helper_playbook.json`` (plan §4.5) and this module becomes
``playbook/loader.py``.
"""
