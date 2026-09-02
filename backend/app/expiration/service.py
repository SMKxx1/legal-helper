"""Expiration orchestration core — extract → Airtable upsert (PLAN §3.10).

The single function every trigger funnels through: :func:`process_pdf`. Given a signed-NDA PDF's bytes
plus its file reference + display name, it runs the extractor (:mod:`app.expiration.extractor`) and, on
a successful date, upserts the MINIMAL Airtable row (:mod:`app.integrations.airtable`). Both capabilities
fail SOFT independently (PLAN §6):

* LLM inference off  → nothing to do (``status='llm_off'``);
* extraction ran but the model couldn't determine a date → no write (``status='no_date'``);
* Airtable off       → the date was extracted but not persisted (``status='airtable_off'``);
* Airtable write blew up (transient/terminal) → ``status='airtable_error'`` (the date is still returned
  so a caller can surface it).

Kept pure + free of Drive/Slack specifics so it is exercised end-to-end with injected transports and
zero network. The archive hook, the nightly sweep, and the manual re-extract command all call this.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..integrations.airtable import (
    AirtableError,
    AirtableUnavailable,
    build_expiration_fields,
    upsert_expiration,
)
from ..telemetry import get_logger
from .extractor import ExpirationUnavailable, extract_expiration

log = get_logger("nda.expiration.service")


@dataclass(frozen=True)
class ExpirationOutcome:
    """What one PDF's extract→upsert pass decided and did.

    ``status`` ∈ ``{written, no_date, airtable_off, airtable_error, llm_off}``. ``date`` is the
    extracted ISO date (or None). ``upserted`` is True only when a row was actually written to Airtable.
    """

    file_ref: str
    date: str | None
    upserted: bool
    status: str
    detail: str = ""


def process_pdf(
    pdf_bytes: bytes,
    *,
    file_ref: str,
    display_name: str,
    settings=None,
    registry=None,
    extract_transport: httpx.BaseTransport | None = None,
    airtable_transport: httpx.BaseTransport | None = None,
) -> ExpirationOutcome:
    """Extract the expiration date from ``pdf_bytes`` and upsert it to Airtable (both fail-soft).

    ``file_ref`` is the stable merge key (the archive Drive file id); ``display_name`` is the
    human-friendly NDA name written alongside the date. The two ``*_transport`` seams inject
    ``httpx.MockTransport`` for tests. Never raises for a capability-off / per-document failure — every
    outcome is a typed :class:`ExpirationOutcome`.
    """
    from ..config import get_settings

    settings = settings or get_settings()

    # 1) Extract (LLM capability gate). A disabled capability is a clean no-op.
    try:
        result = extract_expiration(
            pdf_bytes,
            settings=settings,
            registry=registry,
            transport=extract_transport,
        )
    except ExpirationUnavailable as exc:
        log.info("expiration.process.llm_off", file_ref=file_ref, detail=str(exc))
        return ExpirationOutcome(
            file_ref=file_ref,
            date=None,
            upserted=False,
            status="llm_off",
            detail=str(exc),
        )

    if result.date is None:
        # Extraction ran but the model returned ERROR / an off-contract reply — no date to write. The
        # nightly sweep will retry it (it stays untracked in Airtable), so this is a straggler, not a
        # dead end.
        log.info(
            "expiration.process.no_date",
            file_ref=file_ref,
            extract_status=result.status,
            detail=result.detail,
        )
        return ExpirationOutcome(
            file_ref=file_ref,
            date=None,
            upserted=False,
            status="no_date",
            detail=result.detail or result.status,
        )

    # 2) Upsert the minimal row (Airtable capability gate).
    fields = build_expiration_fields(display_name, result.date)
    try:
        upsert_expiration(
            file_ref,
            fields,
            settings=settings,
            registry=registry,
            transport=airtable_transport,
        )
    except AirtableUnavailable as exc:
        log.info(
            "expiration.process.airtable_off",
            file_ref=file_ref,
            date=result.date,
            detail=str(exc),
        )
        return ExpirationOutcome(
            file_ref=file_ref,
            date=result.date,
            upserted=False,
            status="airtable_off",
            detail=str(exc),
        )
    except AirtableError as exc:
        # A transient/terminal write failure — the date WAS extracted; the sweep retries the write.
        log.warning(
            "expiration.process.airtable_error",
            file_ref=file_ref,
            date=result.date,
            error=repr(exc),
        )
        return ExpirationOutcome(
            file_ref=file_ref,
            date=result.date,
            upserted=False,
            status="airtable_error",
            detail=str(exc),
        )

    log.info("expiration.process.written", file_ref=file_ref, date=result.date)
    return ExpirationOutcome(
        file_ref=file_ref, date=result.date, upserted=True, status="written"
    )


__all__ = ["ExpirationOutcome", "process_pdf"]
