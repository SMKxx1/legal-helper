"""Run-aware span→``{{token}}`` replacement — the generation filler, in reverse.

Where ``app.support_task.generator.fill_docx`` replaces a ``{{token}}`` that may straddle several
formatting runs with its value (collapsing into the first run), :func:`apply_tokenize` replaces an
arbitrary user-highlighted character span — addressed by a ``docview`` locator + ``[start, end)``
offsets over the same normalized run-concatenated text — with a ``{{token_name}}`` run that
inherits the **first covered run's** formatting (its ``rPr`` deep-copied: the filler's convention,
inverted). Runs are split at the span boundaries, so text and formatting on either side of the
selection survive byte-for-byte:

- the first covered run keeps its uncovered prefix (same run element, truncated);
- the last covered run keeps its uncovered suffix (same element truncated, or — when the span
  starts and ends inside ONE run — a new run deep-copied from it, so the tail keeps its exact
  formatting);
- fully-covered *text* runs are removed; zero-width runs inside the span (e.g. a run holding only
  an image) are never touched — a drawing can not be deleted by a text selection.

Everything is refusal-first (see ``app.studio.errors``): a typed error is raised **before any
mutation is saved**, and a post-surgery integrity check re-derives the paragraph text and refuses
to emit bytes that do not read exactly ``text[:start] + "{{token}}" + text[end:]``.

The returned :class:`OpRecord` captures everything undo needs — including a verbatim XML snapshot
of the paragraph *before* surgery. :func:`undo_tokenize` restores that snapshot, which returns the
paragraph (and therefore the document's ``content_hash``) EXACTLY to its prior state — this is
what keeps the oplog's hash chain sound at arbitrary undo depth. The op-record fields
(``replaced_text``, ``locator``, offsets, hashes) remain the human-auditable trail; redo replays
:func:`apply_tokenize` from them and verifies it reproduces the recorded ``new_hash``.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import asdict, dataclass
from io import BytesIO
from typing import Any

from lxml import etree

from .docview import (
    TOKEN_RE,
    content_hash,
    load_document,
    paragraph_text,
    resolve_locator,
)
from .errors import (
    CrossParagraphSpanError,
    EmptySpanError,
    InvalidTokenNameError,
    OpIntegrityError,
    RangeOutOfBoundsError,
    StaleViewError,
    TokenOverlapError,
    UnsupportedSpanError,
)

_TOKEN_NAME_OK = re.compile(r"[A-Za-z0-9_]+")

#: Run children a text-span replacement may safely consume. Anything else inside a covered run
#: (w:drawing, w:pict, w:fldChar, w:footnoteReference, …) makes the span refuse — deleting or
#: re-writing such content via a text selection would corrupt the document.
_SAFE_RUN_CHILDREN = frozenset({"rPr", "t", "tab", "br", "cr", "lastRenderedPageBreak"})


@dataclass(frozen=True)
class OpRecord:
    """The logged, reversible record of one tokenize operation (stored as ``studio_ops.op_json``)."""

    locator: str
    start: int
    end: int
    replaced_text: str  # the exact original span text
    token_name: str
    prior_hash: str  # content hash BEFORE the op (undo restores to this)
    new_hash: str  # content hash AFTER the op (undo/redo concurrency anchor)
    paragraph_xml_before: str  # verbatim <w:p> snapshot — the byte-faithful undo source

    @property
    def token_text(self) -> str:
        return "{{" + self.token_name + "}}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OpRecord:
        return cls(
            locator=data["locator"],
            start=int(data["start"]),
            end=int(data["end"]),
            replaced_text=data["replaced_text"],
            token_name=data["token_name"],
            prior_hash=data["prior_hash"],
            new_hash=data["new_hash"],
            paragraph_xml_before=data["paragraph_xml_before"],
        )


def _save(doc: Any) -> bytes:
    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def _local(el: Any) -> str:
    return etree.QName(el).localname


def _check_covered_run_safe(r_el: Any) -> None:
    for child in r_el:
        name = _local(child)
        if name not in _SAFE_RUN_CHILDREN:
            raise UnsupportedSpanError(f"a covered run contains <w:{name}>")


def _snapshot_paragraph(paragraph_xml: str) -> Any:
    """Parse a recorded ``<w:p>`` snapshot back into a python-docx ``CT_P`` element.

    Parsed with python-docx's own parser (NOT bare lxml) so the element carries the proper oxml
    classes — required both for re-insertion into a live document tree and for text composition.
    """
    from docx.oxml.parser import parse_xml

    return parse_xml(paragraph_xml)


def _plain_para_text(p_el: Any) -> str:
    """Run-concatenated text of a raw ``<w:p>`` element — EXACTLY ``docview.paragraph_text``.

    Delegated to python-docx itself (wrap the element, join ``run.text``) so the composition
    (w:t text, w:tab, w:br/w:cr, w:noBreakHyphen -> "-", w:ptab -> "\\t"; direct-child runs only)
    can never drift from the normalization the op's offsets were computed against.
    """
    from docx.text.paragraph import Paragraph

    # parent=None: text composition never touches the story part (typed as always-present).
    return paragraph_text(Paragraph(p_el, None))  # type: ignore[arg-type]


def _replace_span_with_token_run(
    paragraph: Any, start: int, end: int, token_text: str
) -> None:
    """The run surgery: split boundary runs, drop covered text runs, insert the token run."""
    from docx.text.run import Run

    runs = list(paragraph.runs)
    spans: list[tuple[int, int]] = []
    pos = 0
    for run in runs:
        length = len(run.text)
        spans.append((pos, pos + length))
        pos += length

    # Runs whose TEXT overlaps [start, end). Zero-width runs (images, empty runs) never qualify.
    covered = [i for i, (a, b) in enumerate(spans) if a < b and a < end and start < b]
    if not covered:  # unreachable given a non-empty span; refuse rather than guess
        raise OpIntegrityError("span maps to no runs")
    first, last = covered[0], covered[-1]
    for i in covered:
        _check_covered_run_safe(runs[i]._r)

    prefix = runs[first].text[: start - spans[first][0]]
    suffix = runs[last].text[end - spans[last][0] :]

    # The token run: the FIRST covered run's element deep-copied (rPr and all), content replaced.
    # (Run.text assignment clears the run's content children but preserves its rPr.)
    token_el = deepcopy(runs[first]._r)
    Run(token_el, paragraph).text = token_text

    if first == last and suffix:
        # Span starts AND ends inside one run: the tail becomes a new run copied from the same
        # element, so the suffix keeps the exact formatting it had.
        suffix_el = deepcopy(runs[last]._r)
        Run(suffix_el, paragraph).text = suffix
        runs[last]._r.addnext(suffix_el)
    elif first < last and suffix:
        runs[last].text = suffix  # same element, truncated in place

    if prefix:
        runs[first].text = prefix  # same element, truncated in place
        runs[first]._r.addnext(token_el)
    else:
        runs[first]._r.addprevious(token_el)

    # Remove the fully-consumed run elements (text fully inside the span, nothing kept).
    doomed = [runs[i]._r for i in covered[1:-1]]
    if not prefix:
        doomed.append(runs[first]._r)
    if first < last and not suffix:
        doomed.append(runs[last]._r)
    for el in doomed:
        el.getparent().remove(el)


def apply_tokenize(
    docx_bytes: bytes,
    locator: str,
    start: int,
    end: int,
    token_name: str,
    *,
    expected_hash: str | None = None,
    end_locator: str | None = None,
) -> tuple[bytes, OpRecord]:
    """Replace the ``[start, end)`` span of the paragraph at ``locator`` with ``{{token_name}}``.

    ``expected_hash`` is the ``content_hash`` of the view the offsets came from — pass it to get
    stale-view refusal (the oplog always does). ``end_locator`` lets a caller relay a selection
    that *ended* in a different paragraph and receive the typed cross-paragraph refusal.

    Returns ``(new_docx_bytes, op_record)`` — or raises a :class:`~app.studio.errors.StudioError`
    without producing any bytes. Token *validity* is the caller's business; only the structural
    name shape is enforced here.
    """
    if not _TOKEN_NAME_OK.fullmatch(token_name or ""):
        raise InvalidTokenNameError(token_name)
    prior = content_hash(docx_bytes)
    if expected_hash is not None and expected_hash != prior:
        raise StaleViewError(expected=expected_hash, actual=prior)
    if end_locator is not None and end_locator != locator:
        raise CrossParagraphSpanError(locator, end_locator)

    doc = load_document(docx_bytes)
    paragraph = resolve_locator(doc, locator)
    text = paragraph_text(paragraph)
    if not (0 <= start <= end <= len(text)):
        raise RangeOutOfBoundsError(start, end, len(text))
    replaced = text[start:end]
    if not replaced.strip():
        raise EmptySpanError()
    for match in TOKEN_RE.finditer(text):
        if match.start() < end and start < match.end():
            raise TokenOverlapError(replaced, match.group())

    paragraph_xml_before = etree.tostring(paragraph._p).decode("utf-8")
    token_text = "{{" + token_name + "}}"
    _replace_span_with_token_run(paragraph, start, end, token_text)

    # Integrity gate: never emit bytes whose text is not exactly the requested splice.
    after = paragraph_text(paragraph)
    expected_after = text[:start] + token_text + text[end:]
    if after != expected_after:
        raise OpIntegrityError(
            "post-surgery paragraph text mismatch — refusing to emit",
            details={"expected": expected_after, "actual": after},
        )

    new_bytes = _save(doc)
    record = OpRecord(
        locator=locator,
        start=start,
        end=end,
        replaced_text=replaced,
        token_name=token_name,
        prior_hash=prior,
        new_hash=content_hash(new_bytes),
        paragraph_xml_before=paragraph_xml_before,
    )
    return new_bytes, record


def undo_tokenize(docx_bytes: bytes, record: OpRecord) -> bytes:
    """The exact inverse of the recorded op: put the original span (text AND formatting) back.

    The paragraph's pre-op ``<w:p>`` snapshot is restored verbatim, so the token run is replaced
    by the original replaced_text with its original formatting (for a span that lived inside one
    run this is also precisely "the token run's formatting" — the inheritance convention, both
    ways). The result is verified to hash back to ``record.prior_hash`` — the guarantee that
    keeps arbitrarily deep undo/redo chains consistent.
    """
    current = content_hash(docx_bytes)
    if current != record.new_hash:
        raise StaleViewError(expected=record.new_hash, actual=current)

    doc = load_document(docx_bytes)
    paragraph = resolve_locator(doc, record.locator)
    original_el = _snapshot_paragraph(record.paragraph_xml_before)
    original_text = _plain_para_text(original_el)

    # Defense-in-depth: the record must be self-consistent and match the live paragraph.
    if original_text[record.start : record.end] != record.replaced_text:
        raise OpIntegrityError("op record is self-inconsistent — refusing to undo")
    expected_current = (
        original_text[: record.start] + record.token_text + original_text[record.end :]
    )
    if paragraph_text(paragraph) != expected_current:
        raise OpIntegrityError(
            "live paragraph does not match the recorded operation — refusing to undo",
            details={
                "expected": expected_current,
                "actual": paragraph_text(paragraph),
            },
        )

    paragraph._p.getparent().replace(paragraph._p, original_el)
    out = _save(doc)
    restored = content_hash(out)
    if restored != record.prior_hash:
        raise OpIntegrityError(
            "undo did not restore the recorded prior content hash — refusing to emit",
            details={"expected": record.prior_hash, "actual": restored},
        )
    return out


def redo_tokenize(docx_bytes: bytes, record: OpRecord) -> bytes:
    """Re-apply a previously undone op and verify it reproduces the recorded state exactly."""
    new_bytes, replay = apply_tokenize(
        docx_bytes,
        record.locator,
        record.start,
        record.end,
        record.token_name,
        expected_hash=record.prior_hash,  # redo only ever applies to the exact pre-op state
    )
    if (
        replay.new_hash != record.new_hash
        or replay.replaced_text != record.replaced_text
    ):
        raise OpIntegrityError(
            "redo did not reproduce the recorded operation — refusing to emit",
            details={"expected": record.new_hash, "actual": replay.new_hash},
        )
    return new_bytes
