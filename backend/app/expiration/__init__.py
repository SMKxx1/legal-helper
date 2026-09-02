"""NDA expiration-date extraction → Airtable (PLAN §3.8 alias contract, §3.10 triggers).

The new P4 feature that graduates the n8n "NDA Expiration Benchmark" (workflow ``3epVP6vj2pPbxDdB``)
into production: read a signed NDA PDF with native-PDF vision through the ZDR-pinned ``expiration``
model alias, extract the single agreement expiration date under the strict ``YYYY-MM-DD|ERROR``
contract, and upsert it into the Airtable expiration tracker (minimal fields, PLAN §6).

Three triggers feed the same core (:func:`app.expiration.service.process_pdf`):

* **archive-time** — the archive agent's ``on_archived`` hook (``app.expiration.hooks``);
* **nightly sweep** — a cron job over the archive folder that backfills / retries stragglers
  (``app.expiration.jobs.register_expiration_jobs`` / :func:`run_expiration_sweep`);
* **manual Slack commands** — ``set expiration of <file> to YYYY-MM-DD`` (a no-LLM override) and
  ``re-extract expiration of <file>`` (``app.bot.intents.expiration``).

Nothing here is imported at package load beyond the light result/error types — the extractor (httpx +
the OpenRouter file-part builder) and the service/jobs are imported by their callers explicitly, so
importing this package stays cheap.
"""

from __future__ import annotations

from .extractor import (
    EXPIRATION_MAX_TOKENS,
    EXPIRATION_PROMPT,
    EXPIRATION_TIMEOUT_S,
    ExpirationError,
    ExpirationResult,
    ExpirationUnavailable,
    build_expiration_request,
    extract_expiration,
    is_iso_date,
)

__all__ = [
    "EXPIRATION_MAX_TOKENS",
    "EXPIRATION_PROMPT",
    "EXPIRATION_TIMEOUT_S",
    "ExpirationError",
    "ExpirationResult",
    "ExpirationUnavailable",
    "build_expiration_request",
    "extract_expiration",
    "is_iso_date",
]
