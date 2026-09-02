"""Prompt RELEASE identity — provenance hash over the engine's system prompts (#6).

A short, stable content id for the CURRENT set of engine system-prompt constants, recorded on
each review's ``provenance`` block so an audit can tell WHICH prompt release graded a document.
Bump-free: the id shifts automatically whenever any hashed system prompt (or the review-lens
source) is edited — no manual version bump. Sibling to ``playbook.release.playbook_release_id``.

Cached per process (``lru_cache``): the prompts are module-level constants, so a PROCESS RESTART
picks up any on-disk edit — matching the deploy invariant that a prompt change ships as a new
process, so a running process never mixes two prompt releases.
"""

from __future__ import annotations

import hashlib
import inspect
from functools import lru_cache


@lru_cache(maxsize=1)
def prompt_release_id() -> str:
    """16-hex sha256 over every engine system-prompt constant (+ the review-lens source).

    Each part is hashed as ``name \\x00 value \\x00`` so a reordering or a rename also shifts the
    id. Imports are DEFERRED to call time to avoid an import cycle — ``review_service`` imports
    this module at module load to build the provenance block, while this function reaches back
    into ``review_service`` for ``build_review_lens``.
    """
    from app.engine import coverage_runner, findings, router, verify, walkaway, wholedoc
    from app.engine.review_service import build_review_lens

    parts: list[tuple[str, str]] = [
        ("ROUTER_SYSTEM", router.ROUTER_SYSTEM),
        ("COVERAGE_SYSTEM", coverage_runner.COVERAGE_SYSTEM),
        ("WHOLEDOC_SYSTEM", wholedoc.WHOLEDOC_SYSTEM),
        ("WHOLEDOC_SYSTEM_QUICK", wholedoc.WHOLEDOC_SYSTEM_QUICK),
        ("WHOLEDOC_SYSTEM_TRIAGE", wholedoc.WHOLEDOC_SYSTEM_TRIAGE),
        ("WHOLEDOC_SYSTEM_EDIT", wholedoc.WHOLEDOC_SYSTEM_EDIT),
        ("WHOLEDOC_SYSTEM_EDIT_REDLINES", wholedoc.WHOLEDOC_SYSTEM_EDIT_REDLINES),
        ("FINDING_SYSTEM", findings.FINDING_SYSTEM),
        ("FINDING_SYSTEM_QUICK", findings.FINDING_SYSTEM_QUICK),
        ("RATE_SYSTEM", verify.RATE_SYSTEM),
        ("WALKAWAY_SYSTEM", walkaway.WALKAWAY_SYSTEM),
        # A function, not a constant: hash its SOURCE so a change to the lens text shifts the id.
        ("build_review_lens", inspect.getsource(build_review_lens)),
    ]
    hasher = hashlib.sha256()
    for name, value in parts:
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update((value or "").encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()[:16]
