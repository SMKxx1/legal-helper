"""The live checklist (PLAN §3.7) — token accounting the same way generation counts them.

``analyze`` reports, for a draft .docx: which ``{{tokens}}`` are present (in first-seen document
order), which required tokens are missing, and which present tokens are unknown (with the closest
known name as a typo suggestion).

The scan deliberately reuses the generation side's approach — the token regex family shared with
``app.support_task.generator`` (``_STRIP_RE``) and the envelope unfilled-token guard
(``app.bot.intents.envelope.scan_docx_tokens``): paragraph-joined run text over body + ALL tables
(nested) + every header/footer variant, via ``docview.extract_view`` (whose traversal mirrors the
filler's). Joining runs before matching means a placeholder split across formatting runs is
counted exactly once — so the checklist can never disagree with what ``fill_docx`` will actually
fill or what the envelope guard will flag.

Required/known token lists are plain name strings supplied by the caller (registry rows are the
caller's business — no registry import here).
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from typing import Any

from .docview import TOKEN_NAME_RE, DocumentView, extract_view


def scan_token_names(source: bytes | DocumentView) -> list[str]:
    """Unique ``{{token}}`` names present in a .docx (or a pre-extracted view), first-seen order."""
    view = source if isinstance(source, DocumentView) else extract_view(source)
    seen: list[str] = []
    for segment in view.segments:
        for match in TOKEN_NAME_RE.finditer(segment.text):
            name = match.group(1)
            if name not in seen:
                seen.append(name)
    return seen


def analyze(
    docx_bytes: bytes | DocumentView,
    required_tokens: Sequence[str],
    known_tokens: Sequence[str],
) -> dict[str, Any]:
    """The checklist payload: ``{found, missing_required, unknown: [{name, closest_known}]}``.

    ``found`` and ``missing_required`` preserve their input orders (document order / required-list
    order) so the page renders stably. ``unknown`` carries the closest known token name (difflib,
    cutoff 0.6) or ``None`` when nothing is plausibly close.
    """
    found = scan_token_names(docx_bytes)
    found_set = set(found)
    known = [str(k) for k in known_tokens]
    known_set = set(known)
    unknown: list[dict[str, str | None]] = []
    for name in found:
        if name not in known_set:
            close = difflib.get_close_matches(name, known, n=1, cutoff=0.6)
            unknown.append({"name": name, "closest_known": close[0] if close else None})
    return {
        "found": found,
        "missing_required": [t for t in required_tokens if t not in found_set],
        "unknown": unknown,
    }
