"""Find-and-map assistant (PLAN §3.7) — typed-placeholder detection + one-click batch mapping.

``detect_placeholders`` scans a :class:`~app.studio.docview.DocumentView` for spans that *look*
like hand-typed placeholders in uploaded source documents:

- bracketed text: ``[COMPANY NAME]``, ``[Company Name]``;
- angle-bracketed text: ``<Company>``;
- fill-in-the-blank underscore runs: ``______``;
- ALL-CAPS 3+-word runs inside (round) brackets: ``(GOVERNING LAW STATE)``.

Each candidate carries its locator + char-range (directly usable by ``apply_tokenize``), the
matched text, and a fuzzy-matched suggestion from a **caller-provided** token list of
``(name, label)`` string pairs — the token registry is deliberately not imported here; validity
and the palette contents are the caller's business.

``map_all`` applies a batch of accepted mappings as a chain of individual
:func:`~app.studio.tokenize_ops.apply_tokenize` operations — one op record per mapping, so every
mapping is individually undoable through the oplog. Mappings within one paragraph are applied
right-to-left so all offsets stay valid against the ORIGINAL view they were detected in.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .docview import TOKEN_RE, DocumentView
from .errors import OverlappingMappingsError
from .tokenize_ops import OpRecord, apply_tokenize

#: Minimum fuzzy-match ratio before a token is suggested at all.
_SUGGEST_CUTOFF = 0.55

_SQUARE_RE = re.compile(r"\[([^\[\]{}<>\n]{1,80})\]")
_ANGLE_RE = re.compile(r"<([^<>\[\]{}\n]{1,80})>")
_UNDERSCORE_RE = re.compile(r"_{3,}")
#: 3+ ALL-CAPS words inside round brackets — "(GOVERNING LAW STATE)" but not "(see clause 4)".
_CAPS_PARENS_RE = re.compile(
    r"\(([A-Z0-9][A-Z0-9&.,'/-]*(?:\s+[A-Z0-9][A-Z0-9&.,'/-]*){2,})\)"
)


@dataclass(frozen=True)
class PlaceholderCandidate:
    """One detected typed-placeholder span, ready to hand to ``apply_tokenize``."""

    locator: str
    start: int
    end: int
    matched_text: str
    suggested_token: str | None
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "locator": self.locator,
            "start": self.start,
            "end": self.end,
            "matched_text": self.matched_text,
            "suggested_token": self.suggested_token,
            "score": round(self.score, 3),
        }


@dataclass(frozen=True)
class TokenMapping:
    """One accepted placeholder→token mapping in a ``map_all`` batch."""

    locator: str
    start: int
    end: int
    token_name: str


def _suggest(
    matched_text: str, token_options: Sequence[tuple[str, str]]
) -> tuple[str | None, float]:
    """Fuzzy-match the placeholder's inner text against (name, label) pairs."""
    cleaned = matched_text.strip("[]<>()_ \t").replace("_", " ").strip().lower()
    if not cleaned:
        return None, 0.0
    best_name: str | None = None
    best_score = 0.0
    for name, label in token_options:
        for candidate in (name.replace("_", " ").lower(), (label or "").lower()):
            if not candidate:
                continue
            score = difflib.SequenceMatcher(None, cleaned, candidate).ratio()
            if score > best_score:
                best_score, best_name = score, name
    if best_score < _SUGGEST_CUTOFF:
        return None, best_score
    return best_name, best_score


def _segment_candidates(text: str) -> list[tuple[int, int, str]]:
    """(start, end, matched_text) spans for one segment, deduped and token-overlap-free."""
    token_spans = [(m.start(), m.end()) for m in TOKEN_RE.finditer(text)]
    raw: list[tuple[int, int, str]] = []
    for pattern in (_SQUARE_RE, _ANGLE_RE, _CAPS_PARENS_RE):
        for m in pattern.finditer(text):
            if not re.search(r"[A-Za-z]", m.group(1)):
                continue  # "[42]" / "<->" are not placeholders
            raw.append((m.start(), m.end(), m.group(0)))
    raw.extend((m.start(), m.end(), m.group(0)) for m in _UNDERSCORE_RE.finditer(text))
    # Drop anything overlapping an existing {{token}} (already mapped), then keep a greedy
    # non-overlapping set, earliest-then-longest first.
    raw = [
        (s, e, t)
        for (s, e, t) in raw
        if not any(s < te and ts < e for ts, te in token_spans)
    ]
    raw.sort(key=lambda m: (m[0], -(m[1] - m[0])))
    picked: list[tuple[int, int, str]] = []
    last_end = -1
    for s, e, t in raw:
        if s >= last_end:
            picked.append((s, e, t))
            last_end = e
    return picked


def detect_placeholders(
    view: DocumentView, token_options: Sequence[tuple[str, str]]
) -> list[PlaceholderCandidate]:
    """Scan a document view for typed-placeholder spans with fuzzy token suggestions.

    ``token_options`` is a caller-provided list of ``(name, label)`` string pairs (e.g. straight
    from the token registry rows) — no registry coupling here.
    """
    candidates: list[PlaceholderCandidate] = []
    for segment in view.segments:
        for start, end, matched in _segment_candidates(segment.text):
            suggested, score = _suggest(matched, token_options)
            candidates.append(
                PlaceholderCandidate(
                    locator=segment.locator,
                    start=start,
                    end=end,
                    matched_text=matched,
                    suggested_token=suggested,
                    score=score,
                )
            )
    return candidates


def _as_mapping(m: TokenMapping | Mapping[str, Any]) -> TokenMapping:
    if isinstance(m, TokenMapping):
        return m
    return TokenMapping(
        locator=m["locator"],
        start=int(m["start"]),
        end=int(m["end"]),
        token_name=m["token_name"],
    )


def ordered_mappings(
    mappings: Sequence[TokenMapping | Mapping[str, Any]],
) -> list[TokenMapping]:
    """Normalize + order a batch so sequential application never invalidates an offset.

    Within one paragraph the mappings are applied right-to-left (descending ``start``), so every
    mapping's offsets stay valid against the original view. Overlapping mappings in the same
    paragraph are refused (:class:`OverlappingMappingsError`) before anything is applied.
    """
    typed = [_as_mapping(m) for m in mappings]
    by_locator: dict[str, list[TokenMapping]] = {}
    for m in typed:
        by_locator.setdefault(m.locator, []).append(m)
    ordered: list[TokenMapping] = []
    for locator, group in by_locator.items():
        group.sort(key=lambda m: m.start, reverse=True)
        for later, earlier in zip(group, group[1:], strict=False):
            if (
                earlier.end > later.start
            ):  # ranges intersect (group is start-descending)
                raise OverlappingMappingsError(locator)
        ordered.extend(group)
    return ordered


def map_all(
    docx_bytes: bytes,
    mappings: Sequence[TokenMapping | Mapping[str, Any]],
    *,
    expected_hash: str | None = None,
) -> tuple[bytes, list[OpRecord]]:
    """Apply a batch of accepted mappings; returns final bytes + one op record per mapping.

    The records chain hash-to-hash in applied order, so the oplog can store them as individual
    consecutive operations — each one undoable on its own. Any refusal aborts the whole batch
    with no bytes produced.
    """
    records: list[OpRecord] = []
    current = docx_bytes
    for i, mapping in enumerate(ordered_mappings(mappings)):
        current, record = apply_tokenize(
            current,
            mapping.locator,
            mapping.start,
            mapping.end,
            mapping.token_name,
            # Stale-view refusal happens against the FIRST application (the batch's baseline);
            # later steps chain from bytes only this function has seen.
            expected_hash=expected_hash if i == 0 else None,
        )
        records.append(record)
    return current, records
