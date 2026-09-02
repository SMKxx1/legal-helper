"""Normalized-text identity for the document-reuse cache (Tier 1).

The /v1 cache already keys on ``sha256(file_bytes)`` (exact-byte idempotency), but the
*same* NDA arriving via email (PDF -> text) vs the Word add-in (DOCX -> text) extracts
to slightly different bytes -> a different sha -> a cache miss + a needless paid LLM run.
This module gives a content key that is stable across those formatting/extraction
differences: the document's text, canonicalized.

A re-submission is served from cache ONLY when its normalized text is IDENTICAL — zero
content difference. Near-duplicate (SimHash/Jaccard) matching was deliberately removed:
for legal text a one-word change ("shall not"->"shall", "two years"->"twenty") keeps the
text ~identical yet flips the meaning, so "similar" is exactly the case that must get a
fresh review, not a reused one.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WS = re.compile(r"\s+")
# Strip formatting punctuation (an extraction artifact that differs PDF-vs-DOCX) but PRESERVE a
# small set of MEANING-BEARING symbols, so two NDAs that differ only by one of these don't collapse
# to one cache key and get served each other's review (e.g. "cap of 5%" vs "cap of 5", "$5m" vs "5m",
# "<= 30 days" vs "30 days"). These symbols extract consistently across formats, so keeping them does
# not cause cross-format false misses.
_KEEP = "%$€£¥<>=±≤≥≠−×÷‰"
_NONWORD = re.compile(rf"[^\w\s{re.escape(_KEEP)}]", re.UNICODE)


def normalize_text(text: str) -> str:
    """Canonical identity of a document: NFKC-normalized, lowercased, formatting punctuation
    stripped (meaning-bearing symbols preserved — see _KEEP), whitespace collapsed. Formatting-,
    encoding-, and extraction-level differences collapse to one string (so the same NDA via email
    PDF vs Word DOCX — or NFC vs NFD unicode — produces an identical key)."""
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = _NONWORD.sub(" ", t)
    return _WS.sub(" ", t).strip()


def norm_sha256(text: str) -> str:
    """Tier-1 cache key: sha256 of the normalized text. Empty for a content-free
    (symbol/whitespace-only) document, so such a doc never keys into the cache."""
    norm = normalize_text(text)
    if not norm:
        return ""
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()
