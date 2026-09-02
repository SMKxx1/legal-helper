"""Redline output: round-trip + sanitization (redline/docx_writer + ingestion/redline_extract).

These were the two least-covered modules (docx_writer ~19%, redline_extract 0%) yet they produce the
user-facing tracked-changes .docx. docx_writer WRITES tracked w:ins/w:del; redline_extract READS them
back — so we round-trip the pair: build an accepted-suggestion redline, then reconstruct the original
(changes rejected) and redlined (changes accepted) sides. Plus the _xml_safe control-char guard that
stops a stray control byte from silently dropping a whole redline.
"""

from __future__ import annotations

from types import SimpleNamespace

from docx import Document

from app.ingestion.redline_extract import extract_redline_versions, has_tracked_changes
from app.redline.docx_writer import _xml_safe, build_redlined_docx

_OLD = "the Receiving Party MUST keep it secret forever"
_NEW = "the Receiving Party shall keep it secret for two (2) years"


def _build(tmp_path, issues) -> bytes:
    review = SimpleNamespace(title="Acme NDA", provider="anthropic", model="claude")
    out = build_redlined_docx(review, issues, tmp_path / "redline.docx")
    return out.read_bytes()


def test_accepted_suggestion_round_trips_through_tracked_changes(tmp_path):
    issue = SimpleNamespace(
        status="accepted",
        incoming_text=_OLD,
        suggested_language=_NEW,
        severity="high",
        title="Perpetual term",
        rationale="Term must be bounded.",
        clause_heading="Confidentiality Term",
        clause_number="7",
    )
    data = _build(tmp_path, [issue])

    assert has_tracked_changes(data) is True
    original, redlined = extract_redline_versions(data)
    # Original = changes REJECTED: keeps the tracked deletion, drops the insertion.
    assert _OLD in original and _NEW not in original
    # Redlined = changes ACCEPTED: keeps the insertion, drops the deletion.
    assert _NEW in redlined and _OLD not in redlined


def test_plain_docx_has_no_tracked_changes(tmp_path):
    d = Document()
    d.add_paragraph("a plain clause with no edits")
    p = tmp_path / "plain.docx"
    d.save(str(p))
    assert has_tracked_changes(p.read_bytes()) is False


def test_unaccepted_issue_renders_plain_with_no_tracked_changes(tmp_path):
    # An issue WITHOUT an accepted suggestion renders its incoming text as a PLAIN run (no w:ins/w:del).
    issue = SimpleNamespace(
        status="open",
        incoming_text="some clause text",
        suggested_language="",
        severity="low",
        title="note",
        rationale="",
        clause_heading="Clause",
        clause_number="1",
    )
    assert has_tracked_changes(_build(tmp_path, [issue])) is False


def test_build_redlined_docx_never_aborts_on_one_bad_clause(tmp_path):
    # A control char in one clause's span must NOT abort the whole export (per-clause rollback +
    # _xml_safe) — the document still builds, and the other clause survives.
    good = SimpleNamespace(
        status="accepted",
        incoming_text=_OLD,
        suggested_language=_NEW,
        severity="high",
        title="ok",
        rationale="",
        clause_heading="Good",
        clause_number="1",
    )
    nasty = SimpleNamespace(
        status="accepted",
        incoming_text="x\x00\x08y",  # control bytes
        suggested_language="z\x1f",
        severity="low",
        title="nasty\x00",
        rationale="",
        clause_heading="Bad\x08",
        clause_number="2",
    )
    data = _build(tmp_path, [good, nasty])
    # The document is well-formed and the good clause's tracked change survived.
    _, redlined = extract_redline_versions(data)
    assert _NEW in redlined


def test_xml_safe_strips_illegal_controls_keeps_valid_whitespace():
    assert _xml_safe("a\x00b\x08c\x1fd") == "abcd"  # C0 control chars stripped
    assert (
        _xml_safe("tab\tnl\ncr\r ok") == "tab\tnl\ncr\r ok"
    )  # valid XML whitespace kept
    assert _xml_safe("clean text") == "clean text"
    assert _xml_safe("\ud800lone-surrogate") == "lone-surrogate"  # surrogate stripped
    assert _xml_safe(None) == ""
    assert _xml_safe("") == ""
