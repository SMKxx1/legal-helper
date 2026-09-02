"""Deterministic, off-model word-level redline engine.

This module produces *clean, semantic* redline markup so the frontend CSS in
`layout.css` can style it:

    <span class="redline"> ... <del class="rl-del">removed</del> ...
                                ... <ins class="rl-ins">added</ins> ... </span>

We compute the diff ourselves with :class:`difflib.SequenceMatcher` over
whitespace-preserving tokens and HTML-escape every token, so the output is both
safe to embed and fully under our control.
"""

from __future__ import annotations

import difflib
import html
import re

# Split keeping the whitespace runs as their own tokens so that the redline
# preserves the original spacing exactly when re-joined.
_TOKEN_RE = re.compile(r"(\s+)")


def _tokenize(text: str) -> list[str]:
    """Split *text* into word/whitespace tokens, preserving all whitespace.

    Using a capturing group in :func:`re.split` keeps the separators, so
    ``"".join(_tokenize(text)) == text`` for any input.
    """
    if not text:
        return []
    return [tok for tok in _TOKEN_RE.split(text) if tok != ""]


def _escape_tokens(tokens: list[str]) -> str:
    """HTML-escape and concatenate *tokens* into a safe plain string."""
    return "".join(html.escape(tok) for tok in tokens)


def inline_redline_html(template_text: str, incoming_text: str) -> str:
    """Return safe HTML showing the redline of *template_text* -> *incoming_text*.

    Removed text (present in the template, gone from the incoming doc) is wrapped
    in ``<del class="rl-del">``; added text (new in the incoming doc) in
    ``<ins class="rl-ins">``; unchanged text is rendered as escaped plain text.
    The whole thing is wrapped in ``<span class="redline">`` for styling.
    """
    template_text = template_text or ""
    incoming_text = incoming_text or ""

    old_tokens = _tokenize(template_text)
    new_tokens = _tokenize(incoming_text)

    matcher = difflib.SequenceMatcher(a=old_tokens, b=new_tokens, autojunk=False)
    parts: list[str] = ['<span class="redline">']

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            parts.append(_escape_tokens(old_tokens[i1:i2]))
        elif tag == "delete":
            parts.append(
                f'<del class="rl-del">{_escape_tokens(old_tokens[i1:i2])}</del>'
            )
        elif tag == "insert":
            parts.append(
                f'<ins class="rl-ins">{_escape_tokens(new_tokens[j1:j2])}</ins>'
            )
        elif tag == "replace":
            # Order: show the removed (template) text first, then the added one.
            parts.append(
                f'<del class="rl-del">{_escape_tokens(old_tokens[i1:i2])}</del>'
            )
            parts.append(
                f'<ins class="rl-ins">{_escape_tokens(new_tokens[j1:j2])}</ins>'
            )

    parts.append("</span>")
    return "".join(parts)


def similarity(a: str, b: str) -> float:
    """Return the :class:`difflib.SequenceMatcher` ratio of *a* and *b* (0..1)."""
    return difflib.SequenceMatcher(None, a or "", b or "").ratio()
