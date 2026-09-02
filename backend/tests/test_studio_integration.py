"""End-to-end: studio tokenize → generation fill — the round trip that justifies the studio.

A template author highlights real text in a formatted draft (via the view's offsets), drops
tokens on it (one direct ``apply_tokenize``, one findmap-suggested ``map_all`` batch), the
checklist confirms the doc is generation-ready, and the PORTED generation filler
(``app.support_task.generator.fill_docx``) then fills the freshly-minted tokens — value text
landing with the formatting the token run inherited from the first covered run.
"""

from __future__ import annotations

from conftest_studio import rich_doc

from app.studio.checklist import analyze, scan_token_names
from app.studio.docview import extract_view, load_document, resolve_locator
from app.studio.findmap import detect_placeholders, map_all
from app.studio.tokenize_ops import apply_tokenize
from app.support_task.generator import fill_docx


def test_tokenize_then_generate_end_to_end():
    data = rich_doc()
    view = extract_view(data)

    # 1) direct highlight→drop on a formatted 2-run span: "ACME CORPORATION" in the body
    body = view.find("body/p:0")
    start = body.text.index("ACME CORPORATION")
    data, record = apply_tokenize(
        data,
        "body/p:0",
        start,
        start + len("ACME CORPORATION"),
        "counterparty_name",
        expected_hash=view.content_hash,
    )
    assert record.replaced_text == "ACME CORPORATION"

    # 2) find-and-map the typed placeholder "[EFFECTIVE DATE]" via the assistant
    view = extract_view(data)
    candidates = detect_placeholders(view, [("effective_date", "Effective date")])
    accepted = [c for c in candidates if c.suggested_token == "effective_date"]
    assert [c.matched_text for c in accepted] == ["[EFFECTIVE DATE]"]
    data, records = map_all(
        data,
        [
            {
                "locator": c.locator,
                "start": c.start,
                "end": c.end,
                "token_name": c.suggested_token,
            }
            for c in accepted
        ],
        expected_hash=view.content_hash,
    )
    assert len(records) == 1

    # 3) the checklist now sees a generation-ready doc (counts agree with the filler)
    required = ["counterparty_name", "effective_date", "existing_token"]
    report = analyze(data, required, required)
    assert report["found"] == ["counterparty_name", "effective_date", "existing_token"]
    assert report["missing_required"] == []
    assert report["unknown"] == []

    # the token run inherited the FIRST covered run's formatting (italic "ACME ")
    tokenized_para = resolve_locator(load_document(data), "body/p:0")
    (token_run,) = [r for r in tokenized_para.runs if r.text == "{{counterparty_name}}"]
    assert token_run.italic is True

    # 4) the PORTED generation filler fills the freshly-minted tokens
    filled = fill_docx(
        data,
        {
            "counterparty_name": "Globex LLC",
            "effective_date": "1 July 2026",
            "existing_token": "42",
        },
    )
    assert scan_token_names(filled) == []  # nothing left unfilled anywhere
    result = extract_view(filled)
    assert (
        result.find("body/p:0").text
        == "This agreement is between Globex LLC and the recipient."
    )
    assert result.find("body/p:1").text == "Signed on 1 July 2026 by the parties."
    assert result.find("body/tbl:0:0:0/p:0").text == "Cell with 42"

    # the filled value kept the inherited formatting (fill pass 1 is per-run, rPr preserved)
    filled_para = resolve_locator(load_document(filled), "body/p:0")
    (value_run,) = [r for r in filled_para.runs if r.text == "Globex LLC"]
    assert value_run.italic is True
