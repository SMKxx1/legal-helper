"""The ``review`` intent (PLAN §3.3/§3.4, reference §3.5): the severity-grouped summary, in-process
persistence, the no-attachment reply, the Slack file-fetcher seam, and engine-error mapping.

Zero network, zero LLM: the engine run + text extraction are injected fakes, the Slack download is a
stub :data:`~app.bot.intents.review.SlackFileFetcher`, and persistence rides ``reviews_repo`` pointed at
the throwaway per-test DB (the root ``_isolate_engine_writes`` autouse fixture). The happy path proves a
bot review is persisted exactly like an API review (mode=quick, source_channel, bot actor) and rendered
in the ported HIGH/MEDIUM/LOW format.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.api.errors import EngineError
from app.bot.envelope import AttachmentRef, Envelope
from app.bot.intents import IntentContext
from app.bot.intents.review import (
    MAX_FINDINGS_SHOWN,
    NO_ATTACHMENT_TEXT,
    REVIEW_ACTOR_ID,
    ReviewIntent,
    format_review_summary,
)
from app.bot.router import Classification


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _review_result(findings: list[dict]) -> Any:
    from app.engine.coverage_runner import CoverageReport
    from app.engine.review_service import ReviewResult

    return ReviewResult(
        risk_tier="red",
        adherence_score=72.0,
        perspective="mutual",
        findings=findings,
        coverage=CoverageReport(findings=[]),
    )


def _ctx(env: Envelope) -> IntentContext:
    return IntentContext(envelope=env, classification=Classification(intent="review"))


def _email_env(att: AttachmentRef | None) -> Envelope:
    return Envelope(
        channel="email",
        event_key="email:<r1>",
        text="please review this NDA",
        sender_address="user@corp.com",
        email_message_id="<r1>",
        attachments=(att,) if att else (),
    )


def _slack_env(att: AttachmentRef | None) -> Envelope:
    return Envelope(
        channel="slack",
        event_key="slack:R1",
        text="review this",
        slack_channel="C1",
        slack_thread_ts="1.0",
        attachments=(att,) if att else (),
    )


_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# --------------------------------------------------------------------------- #
# format_review_summary (reference §3.5) — grouping / cap / channel variants
# --------------------------------------------------------------------------- #
def _payload(findings: list[dict], *, tier: str = "red", score: float = 72.0) -> dict:
    return {"risk_tier": tier, "adherence_score": score, "findings": findings}


def test_summary_no_findings_is_clean_message() -> None:
    # The bot runs quick (triage) only — no coverage pass — so the clean wording is HEDGED (#2).
    out = format_review_summary(
        _payload([], tier="green", score=100.0), channel="slack"
    )
    assert "No obvious issues found in a quick (triage) review" in out
    assert "run a deep review for sign-off" in out
    assert "risk tier *green*" in out
    assert "adherence *100/100*" in out


def test_summary_clean_is_confident_when_coverage_ran() -> None:
    # When a coverage pass DID run (deep), the confident "No material issues found" is kept.
    payload = _payload([], tier="green", score=100.0)
    payload["analysis_integrity"] = {"coverage_ran": True, "degraded_components": []}
    out = format_review_summary(payload, channel="slack")
    assert "No material issues found" in out
    assert "quick (triage)" not in out


def test_summary_degradation_warning_is_appended() -> None:
    # A degraded component (#8) appends a caveat that a red/elevated tier may be conservative.
    payload = _payload(
        [{"severity": "high", "title": "Broad indemnity"}], tier="red", score=40.0
    )
    payload["analysis_integrity"] = {
        "coverage_ran": False,
        "degraded_components": ["coverage", "router"],
    }
    out = format_review_summary(payload, channel="slack")
    assert "part of the automated analysis degraded (coverage, router)" in out
    assert "not confirmed legal risk" in out
    # Still lists the finding.
    assert "• Broad indemnity" in out


def test_summary_clean_degradation_warning_is_appended() -> None:
    payload = _payload([], tier="red", score=0.0)
    payload["analysis_integrity"] = {
        "coverage_ran": False,
        "degraded_components": ["coverage"],
    }
    out = format_review_summary(payload, channel="email")
    assert "quick (triage) review" in out
    assert "part of the automated analysis degraded (coverage)" in out
    assert "*" not in out  # email surface has no mrkdwn emphasis


def test_summary_groups_by_severity_slack_mrkdwn() -> None:
    findings = [
        {"severity": "high", "title": "Broad indemnity"},
        {"severity": "high", "title": "Unlimited liability"},
        {"severity": "medium", "title": "Long term"},
        {"severity": "low", "title": "Odd notice address"},
    ]
    out = format_review_summary(_payload(findings), channel="slack")
    assert out.startswith(
        "*NDA review complete* — risk tier *red*, adherence *72/100*."
    )
    assert "*High (2)*" in out
    assert "*Medium (1)*" in out
    assert "*Low (1)*" in out
    assert "• Broad indemnity" in out
    assert "• Odd notice address" in out


def test_summary_email_variant_is_plain_text() -> None:
    findings = [{"severity": "high", "title": "Broad indemnity"}]
    out = format_review_summary(_payload(findings), channel="email")
    assert "*" not in out  # no mrkdwn emphasis on the email surface
    assert "•" not in out
    assert "- Broad indemnity" in out
    assert out.startswith("NDA review complete — risk tier red, adherence 72/100.")


def test_summary_uses_verified_severity_when_present() -> None:
    findings = [{"severity": "low", "verified_severity": "high", "title": "Escalated"}]
    out = format_review_summary(_payload(findings), channel="slack")
    assert "*High (1)*" in out
    assert "*Low" not in out


def test_summary_caps_at_25_with_overflow_line() -> None:
    findings = [{"severity": "high", "title": f"Issue {i}"} for i in range(30)]
    out = format_review_summary(_payload(findings), channel="slack")
    assert out.count("• Issue") == MAX_FINDINGS_SHOWN  # only 25 listed
    assert f"…and {30 - MAX_FINDINGS_SHOWN} more." in out


def test_summary_none_severity_findings_are_excluded() -> None:
    findings = [
        {"severity": "none", "title": "Not material"},
        {"severity": "high", "title": "Material"},
    ]
    out = format_review_summary(_payload(findings), channel="slack")
    assert "Not material" not in out
    assert "*High (1)*" in out


# --------------------------------------------------------------------------- #
# No attachment → ported "attach the NDA" reply (thread recovery is P3)
# --------------------------------------------------------------------------- #
def test_no_attachment_asks_for_the_document() -> None:
    called: list[Any] = []
    intent = ReviewIntent(
        extract_text=lambda *a: called.append(a) or "x",
        run_engine=lambda text: called.append(text) or _review_result([]),
        save_review=lambda *a, **k: called.append("saved"),
    )
    reply = intent(_ctx(_email_env(None)))
    assert reply.text == NO_ATTACHMENT_TEXT
    assert reply.attachments == ()
    assert (
        called == []
    )  # no extraction / engine / persistence when there's nothing to review


# --------------------------------------------------------------------------- #
# Email happy path: real persistence + severity summary
# --------------------------------------------------------------------------- #
def test_email_review_persists_and_summarizes(session_factory, tmp_path) -> None:
    spool = tmp_path / "nda.docx"
    spool.write_bytes(b"PK\x03\x04 spooled docx bytes")
    att = AttachmentRef(
        filename="nda.docx", source_ref=str(spool), content_type=_DOCX_MIME
    )
    captured_text: list[str] = []

    intent = ReviewIntent(
        extract_text=lambda fn, data: (
            captured_text.append(data.decode("latin1"))
            or "MUTUAL NON-DISCLOSURE AGREEMENT ... obligations ..."
        ),
        run_engine=lambda text: _review_result(
            [
                {"severity": "high", "title": "One-sided indemnity"},
                {"severity": "medium", "title": "Perpetual term"},
            ]
        ),
        # serialize + save_review default to the real /v1 path → asserts true persistence parity.
    )
    reply = intent(_ctx(_email_env(att)))

    # Summary rendered from the persisted payload.
    assert reply.text.startswith("NDA review complete — risk tier red")
    assert "- One-sided indemnity" in reply.text
    assert "- Perpetual term" in reply.text
    # The spooled bytes were actually read off disk and handed to extraction.
    assert captured_text and "spooled docx bytes" in captured_text[0]

    from app.models import EngineReview

    with session_factory() as s:
        rows = list(s.query(EngineReview).all())
    assert len(rows) == 1
    row = rows[0]
    assert row.mode == "quick"
    assert row.source_channel == "email"
    assert row.actor_user_id == REVIEW_ACTOR_ID
    assert row.risk_tier == "red"
    assert row.doc_filename == "nda.docx"


# --------------------------------------------------------------------------- #
# Slack path: the injectable file-fetcher seam
# --------------------------------------------------------------------------- #
def test_slack_review_downloads_via_fetcher_seam() -> None:
    fetched: list[AttachmentRef] = []
    saved: list[dict] = []

    def fake_fetch(att: AttachmentRef) -> bytes:
        fetched.append(att)
        return b"PK\x03\x04 slack file bytes"

    att = AttachmentRef(
        filename="nda.docx", source_ref="F0999", content_type=_DOCX_MIME
    )
    intent = ReviewIntent(
        slack_fetch=fake_fetch,
        extract_text=lambda fn, data: f"NDA TEXT ({len(data)} bytes)",
        run_engine=lambda text: _review_result(
            []
        ),  # clean → hedged quick-triage wording
        save_review=lambda payload, **kw: saved.append({"payload": payload, **kw}),
    )
    reply = intent(_ctx(_slack_env(att)))

    assert len(fetched) == 1
    assert (
        fetched[0].source_ref == "F0999"
    )  # the Slack file id was handed to the fetcher
    assert "No obvious issues found in a quick (triage) review" in reply.text
    # Persisted through the SAME shape /v1 uses, tagged with the Slack channel + bot actor.
    assert len(saved) == 1
    assert saved[0]["source_channel"] == "slack"
    assert saved[0]["mode"] == "quick"
    assert saved[0]["actor_user_id"] == REVIEW_ACTOR_ID


def test_slack_fetch_failure_is_a_friendly_reply() -> None:
    def boom(att: AttachmentRef) -> bytes:
        raise RuntimeError("slack download 403")

    att = AttachmentRef(filename="nda.docx", source_ref="Fbad")
    intent = ReviewIntent(
        slack_fetch=boom,
        extract_text=lambda fn, data: "unused",
        run_engine=lambda text: _review_result([]),
        save_review=lambda *a, **k: None,
    )
    reply = intent(_ctx(_slack_env(att)))
    assert "couldn't open that attachment" in reply.text.lower()


# --------------------------------------------------------------------------- #
# Engine / extraction error mapping (reference §3.5 "Review Error")
# --------------------------------------------------------------------------- #
def _intent_with_engine(exc: Exception) -> ReviewIntent:
    return ReviewIntent(
        extract_text=lambda fn, data: "some text",
        run_engine=lambda text: (_ for _ in ()).throw(exc),
        save_review=lambda *a, **k: None,
    )


def _att() -> AttachmentRef:
    return AttachmentRef(filename="nda.docx", source_ref="F1", content_type=_DOCX_MIME)


def _run(intent: ReviewIntent) -> str:
    return intent(
        _ctx(
            Envelope(
                channel="slack",
                event_key="slack:E",
                slack_channel="C1",
                slack_thread_ts="1.0",
                attachments=(_att(),),
            )
        )
    ).text


def test_engine_too_large_maps_to_friendly_text() -> None:
    intent = ReviewIntent(
        slack_fetch=lambda att: b"bytes",
        extract_text=lambda fn, data: "text",
        run_engine=lambda text: (_ for _ in ()).throw(
            EngineError(413, "request_too_large", "big")
        ),
        save_review=lambda *a, **k: None,
    )
    assert "too large" in _run(intent).lower()


def test_extraction_unsupported_maps_to_docx_pdf_hint() -> None:
    intent = ReviewIntent(
        slack_fetch=lambda att: b"bytes",
        extract_text=lambda fn, data: (_ for _ in ()).throw(
            EngineError(415, "unsupported_media_type", "nope")
        ),
        run_engine=lambda text: _review_result([]),
        save_review=lambda *a, **k: None,
    )
    out = _run(intent).lower()
    assert ".docx" in out and ".pdf" in out


def test_unprocessable_maps_to_couldnt_process() -> None:
    intent = ReviewIntent(
        slack_fetch=lambda att: b"bytes",
        extract_text=lambda fn, data: (_ for _ in ()).throw(
            EngineError(422, "unprocessable", "bad")
        ),
        run_engine=lambda text: _review_result([]),
        save_review=lambda *a, **k: None,
    )
    assert "couldn't process" in _run(intent).lower()


def test_empty_text_is_scanned_image_reply() -> None:
    intent = ReviewIntent(
        slack_fetch=lambda att: b"bytes",
        extract_text=lambda fn, data: "   \n  ",
        run_engine=lambda text: _review_result([]),
        save_review=lambda *a, **k: None,
    )
    out = _run(intent).lower()
    assert "scanned" in out


def test_non_engine_error_propagates_for_sweep_retry() -> None:
    # A transient (non-EngineError) engine failure must PROPAGATE so the pipeline records failed and the
    # worker sweep retries — never swallowed into a clean/empty summary.
    intent = ReviewIntent(
        slack_fetch=lambda att: b"bytes",
        extract_text=lambda fn, data: "text",
        run_engine=lambda text: (_ for _ in ()).throw(RuntimeError("provider down")),
        save_review=lambda *a, **k: None,
    )
    with pytest.raises(RuntimeError):
        _run(intent)
