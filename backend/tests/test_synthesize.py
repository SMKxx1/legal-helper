"""Pure-logic tests for the deterministic risk-tier / adherence synthesis (review_service.synthesize).

This is the off-model scoring that turns findings into the headline tier + adherence score the API
returns. It needs no provider or DB. Covers the tier boundaries and the rule that LOW findings are
advisory (excluded from the adherence penalty and never raise the tier).
"""

from __future__ import annotations

from app.engine.coverage_runner import CoverageReport
from app.engine.review_service import synthesize


def _syn(findings, *, cross=None, clause_count=2):
    return synthesize(
        findings, CoverageReport(findings=[]), cross or [], clause_count=clause_count
    )


def test_clean_is_green_and_full_adherence():
    tier, score = _syn([])
    assert tier == "green"
    assert score == 100.0


def test_a_high_finding_is_red_and_penalized():
    tier, score = _syn([{"severity": "high"}])
    assert tier == "red"
    assert score < 100.0


def test_a_medium_finding_is_yellow():
    tier, _ = _syn([{"severity": "medium"}])
    assert tier == "yellow"


def test_low_findings_are_advisory_green_and_unpenalized():
    # LOW findings must not raise the tier nor reduce the adherence score.
    tier, score = _syn([{"severity": "low"}, {"severity": "low"}])
    assert tier == "green"
    assert score == 100.0


def test_verified_severity_overrides_raw():
    # The T4 gate's verified_severity (not the raw severity) drives the tier.
    tier, _ = _syn([{"severity": "high", "verified_severity": "low"}])
    assert tier == "green"


def test_high_cross_clause_flag_is_red():
    tier, _ = _syn([], cross=[{"severity": "high"}])
    assert tier == "red"
