"""PDF layout extraction: per-line bounding boxes mapped to character offsets.

Used to anchor on-screen highlights and suggestion bubbles to exact positions in
the rendered PDF. We build a ``full_text`` from the PDF's text lines (so the
clause segmenter's char offsets line up with the boxes) and expose helpers to
turn a character range into normalised rectangles (fractions of page size, so
the frontend can scale them to any render width).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class PdfLine:
    page: int  # 1-based
    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    char_start: int
    char_end: int


@dataclass(slots=True)
class PdfWord:
    """One word with its box, char offsets, and captured font metadata.

    ``char_start``/``char_end`` index the *same* ``full_text`` stream as
    ``PdfLine`` (so ``full_text[w.char_start:w.char_end] == w.text``), which keeps
    the offset contract intact while adding the word-level geometry + font/size
    the in-place bake's fit rule needs (see ``edit/inline.py``). ``font``/``size``/
    ``color`` are best-effort captures from the underlying char run; the PyMuPDF
    bake re-derives exact values from the document at write time.
    """

    page: int  # 1-based
    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    char_start: int
    char_end: int
    font: str = ""
    size: float = 0.0
    color: str | None = None  # "#rrggbb" when known, else None


@dataclass(slots=True)
class PdfPageInfo:
    number: int
    width: float
    height: float


@dataclass(slots=True)
class PdfLayout:
    full_text: str = ""
    lines: list[PdfLine] = field(default_factory=list)
    words: list[PdfWord] = field(default_factory=list)
    pages: list[PdfPageInfo] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)


def _color_to_hex(value) -> str | None:
    """Best-effort convert a pdfplumber ``non_stroking_color`` to ``#rrggbb``.

    pdfplumber reports color as a tuple in [0,1] — 1 component (gray), 3 (RGB),
    or 4 (CMYK) — or ``None``. Anything we can't read returns ``None`` (the bake
    then defaults to black), so this never raises.
    """
    try:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            value = (value,)
        comps = [float(c) for c in value]
        if len(comps) == 1:
            r = g = b = comps[0]
        elif len(comps) == 3:
            r, g, b = comps
        elif len(comps) == 4:  # CMYK
            c, m, y, k = comps
            r, g, b = (1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)
        else:
            return None
        to255 = lambda f: max(0, min(255, round(f * 255)))  # noqa: E731
        return f"#{to255(r):02x}{to255(g):02x}{to255(b):02x}"
    except Exception:
        return None


def _words_from_line(
    page_no: int, ln: dict, text: str, char_start: int
) -> list[PdfWord]:
    """Build ``PdfWord``s for one extracted line.

    Offsets come from locating each whitespace-delimited token inside ``text``
    (the exact slice that fed ``full_text``), so alignment is guaranteed. Geometry
    and font/size/color are read from the line's underlying ``chars`` when present
    by matching each token's characters in reading order; if chars are missing or
    drift, the token still gets correct offsets with proportionally interpolated
    geometry (good enough for rects; the bake re-derives exact boxes).
    """
    words: list[PdfWord] = []
    chars = ln.get("chars") or []
    # Non-space chars in reading order, for matching token characters to geometry.
    glyphs = [c for c in chars if (c.get("text") or "") != " "]
    gi = 0
    cursor = 0
    lx0, lx1 = float(ln["x0"]), float(ln["x1"])
    ltop, lbot = float(ln["top"]), float(ln["bottom"])
    span = max(1, len(text))
    for token in text.split():
        idx = text.find(token, cursor)
        if idx < 0:
            continue
        cursor = idx + len(token)
        ws = char_start + idx
        we = ws + len(token)
        # Pull this token's glyphs from the char run (best-effort, in order).
        tok_glyphs = []
        for _ in range(len(token)):
            if gi < len(glyphs):
                tok_glyphs.append(glyphs[gi])
                gi += 1
        if tok_glyphs and all("x0" in g for g in tok_glyphs):
            x0 = min(float(g["x0"]) for g in tok_glyphs)
            x1 = max(float(g["x1"]) for g in tok_glyphs)
            top = min(float(g["top"]) for g in tok_glyphs)
            bottom = max(float(g["bottom"]) for g in tok_glyphs)
            first = tok_glyphs[0]
            font = str(first.get("fontname") or "")
            size = float(first.get("size") or 0.0)
            color = _color_to_hex(first.get("non_stroking_color"))
        else:  # interpolate across the line box by character position
            x0 = lx0 + (lx1 - lx0) * (idx / span)
            x1 = lx0 + (lx1 - lx0) * (cursor / span)
            top, bottom, font, size, color = ltop, lbot, "", 0.0, None
        words.append(
            PdfWord(
                page=page_no,
                text=token,
                x0=x0,
                top=top,
                x1=x1,
                bottom=bottom,
                char_start=ws,
                char_end=we,
                font=font,
                size=size,
                color=color,
            )
        )
    return words


def extract_pdf_layout(path: str | Path) -> PdfLayout:
    """Extract text lines + words + boxes from a PDF, with an aligned ``full_text``."""
    import pdfplumber

    lines: list[PdfLine] = []
    words: list[PdfWord] = []
    pages: list[PdfPageInfo] = []
    parts: list[str] = []
    cursor = 0

    with pdfplumber.open(str(path)) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            width = float(page.width or 1.0)
            height = float(page.height or 1.0)
            pages.append(PdfPageInfo(number=page_no, width=width, height=height))
            try:
                text_lines = page.extract_text_lines(layout=False, strip=True)
            except Exception:
                text_lines = []
            for ln in text_lines:
                text = (ln.get("text") or "").strip()
                if not text:
                    continue
                start = cursor
                parts.append(text)
                cursor += len(text)
                end = cursor
                parts.append("\n")
                cursor += 1
                lines.append(
                    PdfLine(
                        page=page_no,
                        text=text,
                        x0=float(ln["x0"]),
                        top=float(ln["top"]),
                        x1=float(ln["x1"]),
                        bottom=float(ln["bottom"]),
                        char_start=start,
                        char_end=end,
                    )
                )
                try:
                    words.extend(_words_from_line(page_no, ln, text, start))
                except Exception:  # words are additive — never break line extraction
                    pass

    return PdfLayout(full_text="".join(parts), lines=lines, words=words, pages=pages)


def _page_size(layout: PdfLayout, page: int) -> tuple[float, float]:
    for p in layout.pages:
        if p.number == page:
            return p.width, p.height
    return 1.0, 1.0


def rects_for_range(
    layout: PdfLayout, start: int, end: int, pad: float = 1.5
) -> list[dict]:
    """Normalised highlight rects (x0,y0,x1,y1 in [0,1]) for lines in [start,end).

    Adjacent lines on the same page keep their own rect (one highlight band per
    line), which reads like a marker pen over the clause.
    """
    rects: list[dict] = []
    for ln in layout.lines:
        # Overlap test between the line span and the requested range.
        if ln.char_end <= start or ln.char_start >= end:
            continue
        w, h = _page_size(layout, ln.page)
        rects.append(
            {
                "page": ln.page,
                "x0": max(0.0, (ln.x0 - pad) / w),
                "y0": max(0.0, (ln.top - pad) / h),
                "x1": min(1.0, (ln.x1 + pad) / w),
                "y1": min(1.0, (ln.bottom + pad) / h),
            }
        )
    return rects


def anchor_for_rects(rects: list[dict]) -> dict | None:
    """A point (normalised) to place a bubble: right margin, first line's height."""
    if not rects:
        return None
    first = rects[0]
    return {"page": first["page"], "x": 0.975, "y": (first["y0"] + first["y1"]) / 2.0}


def words_for_range(layout: PdfLayout, start: int, end: int) -> list[PdfWord]:
    """The ``PdfWord``s whose char span overlaps ``[start, end)``, in order."""
    return [w for w in layout.words if not (w.char_end <= start or w.char_start >= end)]


def box_for_range(layout: PdfLayout, start: int, end: int) -> dict | None:
    """Absolute (PDF-point) bounding box of the run at ``[start, end)``.

    Returns ``{page, x0, top, x1, bottom, font, size, color, single_line}`` in the
    PDF's own coordinate space (top-down, as pdfplumber reports), or ``None`` when
    no words cover the range. The in-place bake uses this to decide fit and to seed
    the redaction rectangle + reinserted-text origin. ``single_line`` is True when
    every covering word shares one page and vertical band (a precondition for a
    clean in-place patch; multi-line runs route to reflow).
    """
    words = words_for_range(layout, start, end)
    if not words:
        return None
    page = words[0].page
    same_page = all(w.page == page for w in words)
    x0 = min(w.x0 for w in words)
    x1 = max(w.x1 for w in words)
    top = min(w.top for w in words)
    bottom = max(w.bottom for w in words)
    # "Single line" ≈ the run's height is within ~1.6× the tallest word height
    # (wrapping onto another line roughly doubles the span).
    line_h = max((w.bottom - w.top) for w in words) or 1.0
    single_line = same_page and (bottom - top) <= line_h * 1.6
    # Representative font/size/color: the first word that captured real metadata.
    font, size, color = "", 0.0, None
    for w in words:
        if w.size:
            font, size, color = w.font, w.size, w.color
            break
    return {
        "page": page,
        "x0": x0,
        "top": top,
        "x1": x1,
        "bottom": bottom,
        "font": font,
        "size": size,
        "color": color,
        "single_line": single_line,
    }
