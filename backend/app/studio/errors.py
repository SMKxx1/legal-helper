"""Typed studio refusal taxonomy — every way the document-surgery layer says "no".

All refusals subclass :class:`app.api.errors.EngineError`, so a studio route can let them propagate
straight into the standard ``{"error": {code, message, details}}`` envelope, while service-layer
callers branch on the concrete types. The ``code`` strings below are the wire contract the wave-B
studio pages depend on — treat them as frozen once shipped:

==============================  ======  =================================================
code                            status  meaning
==============================  ======  =================================================
``studio_bad_docx``             422     bytes are not a readable .docx
``studio_stale_view``           409     view/content hash no longer matches the document
``studio_locator_not_found``    404     locator does not address a paragraph in this doc
``studio_range_out_of_bounds``  422     [start, end) falls outside the paragraph text
``studio_cross_paragraph``      422     selection spans more than one paragraph
``studio_token_overlap``        409     span overlaps an existing ``{{token}}``
``studio_empty_span``           422     empty or whitespace-only selection
``studio_invalid_token_name``   422     token name is not ``[A-Za-z0-9_]+`` (would corrupt)
``studio_unsupported_span``     422     a covered run carries non-text content (image, field…)
``studio_overlapping_mappings`` 422     batch mappings overlap each other
``studio_nothing_to_undo``      409     no applied operation to undo
``studio_nothing_to_redo``      409     no undone (non-dead) operation to redo
``studio_draft_blob_missing``   409     the template version has no draft .docx bytes
``studio_op_integrity``         500     an inverse/replay did not reproduce the recorded state
==============================  ======  =================================================

Every refusal is raised BEFORE any byte of the document is written — apply/undo/redo either
complete fully or leave the draft untouched (the oplog additionally wraps DB writes in one
transaction).
"""

from __future__ import annotations

from app.api.errors import EngineError


class StudioError(EngineError):
    """Base class for all studio refusals (an :class:`EngineError` with a studio_* code)."""


class BadDocxError(StudioError):
    def __init__(self, message: str = "Bytes are not a readable .docx document."):
        super().__init__(422, "studio_bad_docx", message)


class StaleViewError(StudioError):
    """The client's view was built from different document content — re-extract and retry."""

    def __init__(self, expected: str, actual: str):
        super().__init__(
            409,
            "studio_stale_view",
            "The document changed since this view was extracted — refresh the view and retry.",
            details={"expected_hash": expected, "actual_hash": actual},
        )


class LocatorNotFoundError(StudioError):
    def __init__(self, locator: str, reason: str = ""):
        super().__init__(
            404,
            "studio_locator_not_found",
            f"Locator {locator!r} does not address a paragraph in this document"
            + (f" ({reason})" if reason else "")
            + ".",
            details={"locator": locator},
        )


class RangeOutOfBoundsError(StudioError):
    def __init__(self, start: int, end: int, length: int):
        super().__init__(
            422,
            "studio_range_out_of_bounds",
            f"Character range [{start}, {end}) is outside the paragraph text (length {length}).",
            details={"start": start, "end": end, "length": length},
        )


class CrossParagraphSpanError(StudioError):
    def __init__(self, locator: str, end_locator: str):
        super().__init__(
            422,
            "studio_cross_paragraph",
            "A selection must stay inside one paragraph — highlight within a single "
            "paragraph and retry.",
            details={"locator": locator, "end_locator": end_locator},
        )


class TokenOverlapError(StudioError):
    def __init__(self, span_text: str, token: str):
        super().__init__(
            409,
            "studio_token_overlap",
            f"The selection overlaps the existing placeholder {token!r} — "
            "undo or select around it instead.",
            details={"span_text": span_text, "token": token},
        )


class EmptySpanError(StudioError):
    def __init__(self) -> None:
        super().__init__(
            422,
            "studio_empty_span",
            "The selection is empty or whitespace-only — highlight the text to replace.",
        )


class InvalidTokenNameError(StudioError):
    def __init__(self, token_name: str):
        super().__init__(
            422,
            "studio_invalid_token_name",
            f"Token name {token_name!r} is not a bare snake_case identifier "
            "(letters, digits, underscores).",
            details={"token_name": token_name},
        )


class UnsupportedSpanError(StudioError):
    def __init__(self, reason: str):
        super().__init__(
            422,
            "studio_unsupported_span",
            f"The selection covers document content that cannot be safely replaced ({reason}).",
        )


class OverlappingMappingsError(StudioError):
    def __init__(self, locator: str):
        super().__init__(
            422,
            "studio_overlapping_mappings",
            "Two mappings in the batch overlap each other in the same paragraph.",
            details={"locator": locator},
        )


class NothingToUndoError(StudioError):
    def __init__(self) -> None:
        super().__init__(
            409, "studio_nothing_to_undo", "There is no applied operation to undo."
        )


class NothingToRedoError(StudioError):
    def __init__(self) -> None:
        super().__init__(
            409, "studio_nothing_to_redo", "There is no undone operation to redo."
        )


class DraftBlobMissingError(StudioError):
    def __init__(self, template_version_id: str):
        super().__init__(
            409,
            "studio_draft_blob_missing",
            "This template version has no draft .docx bytes loaded.",
            details={"template_version_id": template_version_id},
        )


class OpIntegrityError(StudioError):
    """An inverse/replay failed to reproduce the recorded document state — nothing was written."""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(500, "studio_op_integrity", message, details=details)
