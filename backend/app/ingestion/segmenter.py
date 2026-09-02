"""Clause segmentation.

Take the flat text of a parsed NDA and break it into clause-level
:class:`Clause` segments suitable for diffing against a template. Each clause
carries its section ``number`` (e.g. "3" or "3.1"), a ``heading``, the body
``text``, and character offsets into the source.

The splitter recognises common legal-document structures:
  * numbered sections: "1.", "1.1", "2.3.4 ..."
  * labelled sections: "ARTICLE 1", "Section 3", "Clause 2"
  * short Title-Case / ALL-CAPS heading lines

Any preamble before the first recognised section becomes clause index 0
("Preamble" / "Recitals"). For documents with no recognisable structure we
fall back to paragraph (blank-line) splitting so we never return an empty list
for non-empty input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .parser import ParsedDocument

# A line that begins a numbered section, capturing the number and the rest.
# Examples matched: "1. Foo", "1.1 Foo", "10.2.3. Foo", "9A. Foo" (inserted sub-clause).
_NUMBERED_LINE = re.compile(r"^(?P<num>\d+(?:\.\d+)*[A-Za-z]?)\.?\s+(?P<rest>\S.*)$")

# Markdown emphasis / header markers stripped before section matching, so headings
# written as "**1. Title.**" or "## 1 Title" are still recognised as sections
# (counterparty docx/markdown exports routinely bold their headings).
_MD_MARK = re.compile(r"(?:\*\*|__|\*|_|`)")


def _demark(line: str) -> str:
    s = line.strip()
    s = re.sub(r"^#{1,6}\s*", "", s)  # markdown header hashes
    s = _MD_MARK.sub("", s)  # bold / italic / code markers
    return s.strip()


# Labelled section line, e.g. "ARTICLE 1 - Foo", "Section 3. Foo", "Clause 2.1".
_LABELLED_LINE = re.compile(
    r"^(?P<label>ARTICLE|SECTION|CLAUSE)\s+(?P<num>\d+(?:\.\d+)*)\b[\s.:\-]*(?P<rest>.*)$",
    re.IGNORECASE,
)

# Recital / preamble markers used to label the leading block.
_RECITAL_HINT = re.compile(r"\b(WHEREAS|RECITALS?|NOW,?\s+THEREFORE)\b", re.IGNORECASE)

# Heading-like short line (no number) used as a weaker section boundary.
_HEADING_LINE = re.compile(r"^[A-Z0-9][^.?!]{0,60}$")

_HEADING_SEP = re.compile(r"^[\s.:\-–—]+")


@dataclass(slots=True)
class Clause:
    """A single clause / section extracted from a document."""

    index: int
    number: str
    heading: str
    text: str
    start_char: int = 0
    end_char: int = 0


def _split_heading_body(rest: str) -> tuple[str, str]:
    """Given the text after a section number, split a leading heading from body.

    e.g. "Definition of Confidential Information.\\nFoo bar..." ->
         ("Definition of Confidential Information", "Foo bar...")
    """
    rest = rest.strip()
    if not rest:
        return "", ""
    # If the section number was on its own line with the heading, the heading
    # is the first line; the body follows on subsequent lines.
    newline = rest.find("\n")
    if newline != -1:
        first, remainder = rest[:newline], rest[newline + 1 :]
        first_stripped = first.strip().rstrip(".").strip()
        # Treat the first line as a heading when it is short-ish and title-like.
        if first_stripped and len(first_stripped.split()) <= 12:
            return first_stripped, remainder.strip()
        return "", rest
    # Single line: heading may be a leading phrase ending in a period.
    m = re.match(r"(?P<head>[^.]{1,80})\.\s+(?P<body>\S.*)$", rest, re.DOTALL)
    if m:
        return m.group("head").strip(), m.group("body").strip()
    # Otherwise the whole thing is a short heading with no body yet.
    head = rest.rstrip(".").strip()
    if len(head.split()) <= 12:
        return head, ""
    return "", rest


def _heading_for_recitals(text: str) -> str:
    return "Recitals" if _RECITAL_HINT.search(text) else "Preamble"


def _find_section_starts(lines: list[str]) -> list[tuple[int, str, str]]:
    """Return (line_index, number, heading_rest) for each section boundary."""
    starts: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines):
        stripped = _demark(line)
        if not stripped:
            continue
        m = _NUMBERED_LINE.match(stripped)
        if m:
            starts.append((i, m.group("num"), m.group("rest")))
            continue
        m = _LABELLED_LINE.match(stripped)
        if m:
            rest = m.group("rest").strip()
            label = m.group("label").upper()
            heading = rest if rest else label.title()
            starts.append((i, m.group("num"), heading))
            continue
    return starts


def _line_offsets(lines: list[str]) -> list[int]:
    """Prefix-sum of line-start char offsets: ``offsets[i]`` is the start char of line ``i`` in the
    "\\n"-joined text (each line contributes len+1 for its trailing newline). O(N) — deriving each
    offset by re-summing all prior lines is O(N^2), which a crafted many-section document turns into
    an algorithmic DoS."""
    offsets = [0] * (len(lines) + 1)
    for i, ln in enumerate(lines):
        offsets[i + 1] = offsets[i] + len(ln) + 1
    return offsets


def _segment_by_sections(
    text: str, starts: list[tuple[int, str, str]], lines: list[str]
) -> list[Clause]:
    clauses: list[Clause] = []
    idx = 0
    offsets = _line_offsets(lines)

    first_start_line = starts[0][0]
    preamble = "\n".join(lines[:first_start_line]).strip()
    if preamble:
        end = offsets[first_start_line]
        # Trim trailing whitespace offset back onto the real content length.
        clauses.append(
            Clause(
                index=idx,
                number="",
                heading=_heading_for_recitals(preamble),
                text=preamble,
                start_char=0,
                end_char=min(end, len(text)),
            )
        )
        idx += 1

    for n, (line_idx, number, rest) in enumerate(starts):
        next_line = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        body_block = "\n".join(lines[line_idx:next_line]).strip()

        # Re-derive heading/body from the section block. The block begins with
        # the number line; strip the leading number token before splitting.
        first_line = _demark(lines[line_idx])
        nm = _NUMBERED_LINE.match(first_line) or _LABELLED_LINE.match(first_line)
        after_num = first_line
        if nm:
            after_num = _HEADING_SEP.sub("", first_line[nm.end("num") :])
        remaining_lines = "\n".join(lines[line_idx + 1 : next_line]).strip()
        combined_rest = (
            after_num + ("\n" + remaining_lines if remaining_lines else "")
        ).strip()
        heading, body = _split_heading_body(combined_rest)
        if not heading:
            heading = rest.strip().rstrip(".").strip()[:80] or f"Section {number}"
        heading = _demark(heading).rstrip(".").strip() or f"Section {number}"

        start_char = offsets[line_idx]
        end_char = offsets[next_line] if next_line < len(lines) else len(text)
        clauses.append(
            Clause(
                index=idx,
                number=number,
                heading=heading,
                text=body_block,
                start_char=min(start_char, len(text)),
                end_char=min(end_char, len(text)),
            )
        )
        idx += 1

    return clauses


def _segment_by_paragraphs(text: str) -> list[Clause]:
    """Fallback: split on blank lines into paragraph clauses."""
    clauses: list[Clause] = []
    idx = 0
    cursor = 0
    for chunk in re.split(r"\n\s*\n", text):
        stripped = chunk.strip()
        if not stripped:
            cursor += len(chunk) + 2
            continue
        start = text.find(stripped, cursor)
        if start < 0:
            start = cursor
        end = start + len(stripped)
        cursor = end
        # Use the first line as a heading hint when it looks like one.
        first_line = stripped.splitlines()[0].strip()
        heading = ""
        if _HEADING_LINE.match(first_line) and len(first_line.split()) <= 10:
            heading = first_line.rstrip(".").strip()
        clauses.append(
            Clause(
                index=idx,
                number="",
                heading=heading or f"Paragraph {idx + 1}",
                text=stripped,
                start_char=start,
                end_char=end,
            )
        )
        idx += 1
    if not clauses:
        clauses.append(
            Clause(
                index=0,
                number="",
                heading="Document",
                text=text.strip(),
                start_char=0,
                end_char=len(text),
            )
        )
    return clauses


def segment_clauses(source: str | ParsedDocument) -> list[Clause]:
    """Segment ``source`` (raw text or :class:`ParsedDocument`) into clauses."""
    text = source.full_text if isinstance(source, ParsedDocument) else source
    if text is None or not text.strip():
        return []

    lines = text.split("\n")
    starts = _find_section_starts(lines)
    if starts:
        return _segment_by_sections(text, starts, lines)
    return _segment_by_paragraphs(text)
