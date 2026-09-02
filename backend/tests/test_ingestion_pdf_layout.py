"""Tests for app.ingestion.pdf_layout.

``extract_pdf_layout`` is the live path the PDF-source redline writer (redline/docx_writer.py) depends
on; its core contract is that the char offsets on every line/word index the SAME ``full_text`` stream it
returns (so highlight rects and the redline bake line up). That offset invariant is the thing most likely
to break silently, so it is asserted against a REAL generated PDF. The pure rect/box geometry helpers are
covered with a small synthetic layout.
"""

from __future__ import annotations

from pathlib import Path

from app.ingestion.pdf_layout import (
    PdfLayout,
    PdfLine,
    PdfPageInfo,
    PdfWord,
    _color_to_hex,
    anchor_for_rects,
    box_for_range,
    extract_pdf_layout,
    rects_for_range,
    words_for_range,
)

# --------------------------------------------------------------------------- #
# extract_pdf_layout against a real PDF — the offset invariant
# --------------------------------------------------------------------------- #


def _build_pdf(path: Path) -> None:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(
        0,
        8,
        "Section 1. Confidentiality. The Receiving Party shall protect the "
        "Confidential Information and shall not disclose it to any third party.",
    )
    pdf.output(str(path))


def test_extract_pdf_layout_offsets_index_full_text(tmp_path):
    path = tmp_path / "nda.pdf"
    _build_pdf(path)

    layout = extract_pdf_layout(path)

    assert layout.page_count >= 1
    assert layout.pages[0].width > 0 and layout.pages[0].height > 0
    assert layout.lines, "expected extracted lines"

    # The contract: each line/word slice of full_text equals its own text.
    for ln in layout.lines:
        assert layout.full_text[ln.char_start : ln.char_end] == ln.text
    for w in layout.words:
        assert layout.full_text[w.char_start : w.char_end] == w.text

    assert "Confidential" in layout.full_text


# --------------------------------------------------------------------------- #
# _color_to_hex — pdfplumber color tuples -> #rrggbb
# --------------------------------------------------------------------------- #


def test_color_to_hex_handles_gray_rgb_cmyk_and_bad_input():
    assert _color_to_hex(None) is None
    assert _color_to_hex(0.0) == "#000000"  # 1-component gray
    assert _color_to_hex((1, 0, 0)) == "#ff0000"  # RGB
    assert _color_to_hex((0, 0, 0, 0)) == "#ffffff"  # CMYK all-zero -> white
    assert _color_to_hex("not-a-color") is None  # unparseable -> None, never raises
    assert _color_to_hex((1, 2, 3, 4, 5)) is None  # unsupported component count


# --------------------------------------------------------------------------- #
# Pure geometry helpers over a synthetic layout
# --------------------------------------------------------------------------- #


def _layout() -> PdfLayout:
    page = PdfPageInfo(number=1, width=100.0, height=200.0)
    line = PdfLine(
        page=1,
        text="hello",
        x0=10.0,
        top=20.0,
        x1=60.0,
        bottom=30.0,
        char_start=0,
        char_end=5,
    )
    word = PdfWord(
        page=1,
        text="hello",
        x0=10.0,
        top=20.0,
        x1=60.0,
        bottom=30.0,
        char_start=0,
        char_end=5,
        font="F1",
        size=12.0,
        color="#000000",
    )
    return PdfLayout(full_text="hello\n", lines=[line], words=[word], pages=[page])


def test_rects_for_range_selects_overlap_and_normalizes_to_unit_square():
    layout = _layout()
    rects = rects_for_range(layout, 0, 5)
    assert len(rects) == 1
    r = rects[0]
    assert r["page"] == 1
    # Normalized into [0,1] by page size (100 x 200).
    assert 0.0 <= r["x0"] < r["x1"] <= 1.0
    assert 0.0 <= r["y0"] < r["y1"] <= 1.0

    # A range past the line span selects nothing.
    assert rects_for_range(layout, 100, 200) == []


def test_words_and_box_for_range():
    layout = _layout()
    assert [w.text for w in words_for_range(layout, 0, 5)] == ["hello"]
    assert words_for_range(layout, 10, 20) == []  # no overlap

    box = box_for_range(layout, 0, 5)
    assert box is not None
    assert box["page"] == 1
    assert box["single_line"] is True
    assert box["size"] == 12.0
    assert box_for_range(layout, 10, 20) is None


def test_anchor_for_rects():
    layout = _layout()
    rects = rects_for_range(layout, 0, 5)
    anchor = anchor_for_rects(rects)
    assert anchor is not None
    assert anchor["page"] == 1
    assert anchor["x"] == 0.975
    assert 0.0 <= anchor["y"] <= 1.0
    assert anchor_for_rects([]) is None
