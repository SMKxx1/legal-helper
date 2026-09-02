"""Regression tests for the false-GREEN safety fix.

In every production tier ``clause_pass=False``, so the whole-document pass is the SOLE deviation-
finding source. Before the fix, ``run_wholedoc`` swallowed any provider/schema failure into
``([], 0.0)`` — so a document the engine could not analyze was reported as a clean, 100%-adherence
NDA. These tests pin the corrected contract: a hard failure PROPAGATES instead of certifying an
unread document.
"""

from __future__ import annotations

import pytest

from app.ai.gateway import TerminalProviderError
from app.engine.review_service import run_review
from app.engine.wholedoc import run_wholedoc

_DOC = (
    "Section 1. Confidentiality. The Receiving Party shall keep the Confidential "
    "Information secret and shall not disclose it to any third party.\n"
    "Section 2. Term. This Agreement lasts for three (3) years."
)


class _FailingGateway:
    """Minimal Gateway stand-in whose provider call always fails (after the gateway's own retries)."""

    name = "fake"

    def run(self, req, *, eval_mode=False, **kwargs):
        raise TerminalProviderError("provider unavailable")


def test_run_wholedoc_propagates_provider_failure():
    # Was: returned ([], 0.0) — an unanalyzable document looked like a clean NDA. Now: it raises.
    with pytest.raises(TerminalProviderError):
        run_wholedoc(
            _FailingGateway(),
            standard_text="Standard NDA text.",
            incoming_text=_DOC,
            playbook={},
            playbook_version="v-test",
            playbook_block="POSITIONS",
            style="triage",
        )


def test_run_review_errors_instead_of_false_green_when_sole_pass_fails():
    # Production config (clause_pass=False, whole_doc=True): the whole-doc pass is the only finding
    # source, so its failure must surface as an error — NEVER a green/100%-adherence ReviewResult.
    with pytest.raises(TerminalProviderError):
        run_review(
            _FailingGateway(),
            incoming_text=_DOC,
            standard_text="Standard NDA text.",
            playbook={},
            playbook_version="v-test",
            clause_pass=False,
            whole_doc=True,
            skip_coverage=True,
            self_verify=False,
            gw_router=None,
        )
