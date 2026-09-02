"""Expiration extraction eval — the ported n8n "NDA Expiration Benchmark" (id ``3epVP6vj2pPbxDdB``).

Two modes, one scorer:

* **CI mode (default, zero network)** — runs the production extractor against fixture PDFs with a
  ``httpx.MockTransport`` and asserts the GOLDEN REQUEST SHAPE the benchmark pinned (the ``file-parser``
  plugin with ``pdf.engine=native``, the ZDR ``google-vertex`` provider pin, the WITHHELD
  ``document.pdf`` filename, the verbatim 3-step prompt), then scores the canned replies exactly like
  the benchmark (``match`` = exact ISO-string equality; ``ERROR`` counted separately; ``dayDiff`` for
  near-misses). This locks the contract without spending a token.

* **Real-provider mode (opt-in, env-gated — SKIPPED by default)** — when
  ``EXPIRATION_EVAL_REAL=1`` and ``OPENROUTER_API_KEY`` are set, runs the REAL extractor over NDA PDFs
  in ``EXPIRATION_EVAL_PDF_DIR`` scored against an answer-key JSON (``EXPIRATION_EVAL_ANSWER_KEY``, or an
  ``answer_key*.json`` in the PDF dir), mirroring the benchmark's scoring + reporting, and asserts
  accuracy ≥ ``EXPIRATION_EVAL_MIN_ACCURACY`` (default 0.6). This is the graduation gate for when the
  OpenRouter key + the benchmark dataset land (PLAN §9 P4: "expiration eval ≥ benchmark accuracy").

The answer-key format matches the benchmark's Drive JSON: ``{"records": [{"file": "<name>",
"expiration_date": "YYYY-MM-DD", ...}]}`` (or a bare top-level array); each record keyed by ``file``.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.expiration.extractor import (
    EXPIRATION_PROMPT,
    extract_expiration,
    is_iso_date,
)


# --------------------------------------------------------------------------- #
# Scoring — the benchmark's Score Gemini logic, verbatim
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Scored:
    file: str
    predicted: str  # the model's answer, or "ERROR"
    expected: str
    match: bool
    day_diff: int | None  # signed day delta when both are ISO; else None
    is_error: bool  # predicted == "ERROR"


def score_one(file: str, predicted: str | None, expected: str) -> Scored:
    """Score one file exactly as the benchmark's ``Score Gemini`` node does.

    ``predicted`` defaults to ``ERROR`` on any failure/None (the benchmark default). ``match`` is EXACT
    ISO-string equality. ``day_diff`` (signed days) is computed only when both are valid ISO — used to
    distinguish near-misses from total failures; it does NOT affect ``match``.
    """
    pred = (predicted or "ERROR").strip() or "ERROR"
    match = bool(expected) and pred == expected
    day_diff: int | None = None
    if is_iso_date(pred) and is_iso_date(expected):
        from datetime import date

        p = date.fromisoformat(pred)
        e = date.fromisoformat(expected)
        day_diff = (p - e).days
    return Scored(
        file=file,
        predicted=pred,
        expected=expected,
        match=match,
        day_diff=day_diff,
        is_error=(pred == "ERROR"),
    )


def summarize(rows: list[Scored]) -> dict:
    """The benchmark's aggregate report: n, correct, accuracyPct, errors, and the misses list."""
    n = len(rows)
    correct = sum(1 for r in rows if r.match)
    errors = sum(1 for r in rows if r.is_error)
    return {
        "n": n,
        "correct": correct,
        "accuracy": (correct / n) if n else 0.0,
        "errors": errors,
        "misses": [
            {
                "file": r.file,
                "predicted": r.predicted,
                "expected": r.expected,
                "day_diff": r.day_diff,
            }
            for r in rows
            if not r.match
        ],
    }


# --------------------------------------------------------------------------- #
# CI mode — golden request shape + scoring on fixtures (zero network)
# --------------------------------------------------------------------------- #
def _mini_pdf(tag: bytes) -> bytes:
    """A tiny distinct byte payload standing in for an NDA PDF (never parsed in CI mode)."""
    return b"%PDF-1.4\n" + tag + b"\n%%EOF"


#: (file name, pdf bytes, the model's canned reply, the ground-truth date). The reply is what the
#: FakeTransport returns; scoring compares it to the expected date, exactly like the benchmark.
FIXTURES: list[tuple[str, bytes, str, str]] = [
    ("SG_Company_NA__01.pdf", _mini_pdf(b"sg-company-01"), "2027-03-15", "2027-03-15"),
    (
        "US_Individual_Mutual__02.pdf",
        _mini_pdf(b"us-indiv-02"),
        "2026-12-31",
        "2026-12-31",
    ),
    (
        "US_ServiceProvider_NA__03.pdf",
        _mini_pdf(b"us-sp-03"),
        "2028-06-01",
        "2028-06-01",
    ),
    # A hard one where the model can't determine the date -> ERROR (a legitimate benchmark outcome).
    (
        "SG_Individual_Unilateral__04.pdf",
        _mini_pdf(b"sg-indiv-04"),
        "ERROR",
        "2025-09-09",
    ),
]


def _fake_transport_returning(
    reply: str, captured: list[httpx.Request]
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "model": "google/gemini-3.5-flash",
                "choices": [{"message": {"content": reply}}],
            },
        )

    return httpx.MockTransport(handler)


def _ci_settings() -> Settings:
    return Settings(_env_file=None, openrouter_api_key="sk-or-test")


def test_golden_request_shape_over_fixtures() -> None:
    settings = _ci_settings()
    for name, pdf, reply, _expected in FIXTURES:
        captured: list[httpx.Request] = []
        extract_expiration(
            pdf, settings=settings, transport=_fake_transport_returning(reply, captured)
        )
        assert len(captured) == 1, f"{name}: exactly one call"
        body = json.loads(captured[0].content.decode())

        # The benchmark's winning recipe — pinned per fixture.
        assert body["plugins"] == [{"id": "file-parser", "pdf": {"engine": "native"}}]
        assert body["provider"]["zdr"] is True
        assert body["provider"]["data_collection"] == "deny"
        assert body["provider"]["allow_fallbacks"] is False
        assert body["provider"]["only"] == ["google-vertex"]
        assert body["reasoning"] == {"effort": "low", "exclude": True}

        content = body["messages"][0]["content"]
        assert content[0]["text"] == EXPIRATION_PROMPT  # verbatim 3-step prompt
        file_part = content[1]["file"]
        # The real filename is WITHHELD — every request carries the generic document.pdf (anti-cheat).
        assert file_part["filename"] == "document.pdf"
        assert name not in file_part["file_data"]
        # The PDF bytes round-trip through the data URI.
        b64 = file_part["file_data"].split(",", 1)[1]
        assert base64.b64decode(b64) == pdf


def test_fixture_scoring_matches_benchmark_logic() -> None:
    settings = _ci_settings()
    rows: list[Scored] = []
    for name, pdf, reply, expected in FIXTURES:
        result = extract_expiration(
            pdf, settings=settings, transport=_fake_transport_returning(reply, [])
        )
        rows.append(score_one(name, result.date, expected))

    report = summarize(rows)
    # Three fixtures return the correct ISO date; the fourth returns ERROR (a miss + an error).
    assert report["n"] == 4
    assert report["correct"] == 3
    assert report["errors"] == 1
    assert report["accuracy"] == pytest.approx(0.75)
    # The ERROR fixture is the sole miss, with no day_diff (predicted isn't a date).
    assert report["misses"] == [
        {
            "file": "SG_Individual_Unilateral__04.pdf",
            "predicted": "ERROR",
            "expected": "2025-09-09",
            "day_diff": None,
        }
    ]


def test_scorer_day_diff_for_near_miss() -> None:
    # A near-miss (off by a few days) is NOT a match but carries a signed day_diff for diagnostics.
    s = score_one("x.pdf", "2027-03-18", "2027-03-15")
    assert s.match is False
    assert s.day_diff == 3


# --------------------------------------------------------------------------- #
# Real-provider mode — opt-in, env-gated, SKIPPED by default
# --------------------------------------------------------------------------- #
def _load_answer_key(pdf_dir: Path) -> dict[str, str]:
    """Load ``{file -> expiration_date}`` from the answer-key JSON (the benchmark's Drive format)."""
    explicit = os.environ.get("EXPIRATION_EVAL_ANSWER_KEY")
    path = Path(explicit) if explicit else None
    if path is None:
        candidates = sorted(pdf_dir.glob("*answer_key*.json"))
        path = candidates[0] if candidates else None
    if path is None or not path.exists():
        return {}
    data = json.loads(path.read_text())
    records = data.get("records") if isinstance(data, dict) else data
    key: dict[str, str] = {}
    for rec in records or []:
        f = rec.get("file")
        if f:
            key[str(f)] = str(rec.get("expiration_date") or "")
    return key


@pytest.mark.skipif(
    os.environ.get("EXPIRATION_EVAL_REAL") != "1"
    or not os.environ.get("OPENROUTER_API_KEY"),
    reason="real-provider eval is opt-in: set EXPIRATION_EVAL_REAL=1 + OPENROUTER_API_KEY "
    "(+ EXPIRATION_EVAL_PDF_DIR) to run the benchmark against the live model",
)
def test_real_provider_benchmark() -> (
    None
):  # pragma: no cover - opt-in, needs a live key + dataset
    pdf_dir = Path(os.environ.get("EXPIRATION_EVAL_PDF_DIR", "")).expanduser()
    assert pdf_dir.is_dir(), (
        "set EXPIRATION_EVAL_PDF_DIR to the folder of benchmark NDA PDFs"
    )
    answer_key = _load_answer_key(pdf_dir)
    assert answer_key, (
        "no answer key found (EXPIRATION_EVAL_ANSWER_KEY or *answer_key*.json in the dir)"
    )

    # Real settings from the environment (the benchmark pins remain the defaults in config).
    settings = Settings()
    min_accuracy = float(os.environ.get("EXPIRATION_EVAL_MIN_ACCURACY", "0.6"))

    rows: list[Scored] = []
    for pdf_path in sorted(pdf_dir.glob("*.pdf")):
        expected = answer_key.get(pdf_path.name)
        if expected is None:
            continue  # no ground truth for this file — skip (matches the benchmark's join)
        result = extract_expiration(pdf_path.read_bytes(), settings=settings)
        rows.append(score_one(pdf_path.name, result.date, expected))

    report = summarize(rows)
    print(  # surfaced with -s for the operator running the eval
        f"\nexpiration eval: {report['correct']}/{report['n']} "
        f"= {report['accuracy']:.1%} (errors={report['errors']}); misses={report['misses']}"
    )
    assert report["n"] > 0, (
        "no scored files (name mismatch between PDFs and the answer key?)"
    )
    assert report["accuracy"] >= min_accuracy, (
        f"accuracy {report['accuracy']:.1%} below floor {min_accuracy:.1%}"
    )
