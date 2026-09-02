"""The ``expiration`` intent — manual Slack commands over the expiration tracker (PLAN §3.10 trigger c).

Two deterministic, no-classifier commands a human can type at the bot:

* **Manual override** — ``set expiration of <file> to YYYY-MM-DD`` (and reasonable phrasings). Writes
  the given date straight to Airtable with NO LLM call — the human is the source of truth. Always
  available whenever the Airtable tracker is on (no Drive / no model needed).
* **Re-extract** — ``re-extract expiration of <file>`` (and phrasings). Re-runs the PDF extractor for
  one archived NDA (fetch from the archive → :func:`app.expiration.service.process_pdf`), for when the
  automatic pass produced ``ERROR`` or the wrong date.

Parsing lives HERE (``parse_expiration_command`` / ``matches_expiration_command``) as the SINGLE source
of truth, so the handler and — once wired — the deterministic router share one implementation. This
wave registers the handler under the ``expiration`` intent; the router itself is frozen this wave, so
the one-line router change that makes these commands ROUTE (a deterministic branch calling
:func:`matches_expiration_command`, plus adding ``"expiration"`` to ``INTENTS``) is noted for the
integrator in the task open_items.

The handler is the channel-agnostic ``(IntentContext) -> IntentReply`` shape; its side-effecting deps
(the Airtable upsert, the extract→upsert core, the archive-source resolver) are injected constructor
seams so the whole command matrix is unit-tested with stubs and zero network (PLAN house rules).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ...telemetry import get_logger
from . import IntentContext, IntentReply

log = get_logger("nda.bot.intent.expiration")

_ISO = r"\d{4}-\d{2}-\d{2}"

# --------------------------------------------------------------------------- #
# Command grammar (the single source of truth — the router integrator reuses this)
# --------------------------------------------------------------------------- #
#: "set expiration [date] of/for <file> to <YYYY-MM-DD>"
_SET_OF_RE = re.compile(
    rf"\bset\s+(?:the\s+)?expiration(?:\s+date)?\s+(?:of|for)\s+"
    rf"(?P<file>.+?)\s+to\s+(?P<date>{_ISO})\b",
    re.IGNORECASE,
)
#: "set <file> expiration [date] to <YYYY-MM-DD>"  (file named first)
_SET_FILE_FIRST_RE = re.compile(
    rf"\bset\s+(?P<file>.+?)\s+expiration(?:\s+date)?\s+to\s+(?P<date>{_ISO})\b",
    re.IGNORECASE,
)
#: "expiration [date] of/for <file> is/= <YYYY-MM-DD>"  (a bare assertion)
_SET_IS_RE = re.compile(
    rf"\bexpiration(?:\s+date)?\s+(?:of|for)\s+(?P<file>.+?)\s+(?:is|=|:)\s+"
    rf"(?P<date>{_ISO})\b",
    re.IGNORECASE,
)
#: "re-extract | reextract | re-run expiration [date] of/for/on <file>"
_REEXTRACT_RE = re.compile(
    r"\bre-?(?:extract|run)\s+(?:the\s+)?expiration(?:\s+date)?\s+"
    r"(?:of|for|on)\s+(?P<file>.+?)\s*$",
    re.IGNORECASE,
)

_SET_RES = (_SET_OF_RE, _SET_FILE_FIRST_RE, _SET_IS_RE)


@dataclass(frozen=True)
class ExpirationCommand:
    """A parsed manual command: ``action`` ∈ ``{set, reextract}``, the named ``file``, and (for
    ``set``) the ISO ``date``."""

    action: str
    file: str
    date: str = ""


def _clean_file(token: str) -> str:
    """Trim a named-file token: strip surrounding quotes/backticks/angle-brackets + trailing filler."""
    t = (token or "").strip()
    # Slack link/mention wrappers and quotes around a filename.
    t = t.strip("<>").strip().strip("\"'`").strip()
    # A trailing "please"/"thanks" a user may append is not part of the filename.
    t = re.sub(
        r"\s+(?:please|thanks|thank you|thx)\s*$", "", t, flags=re.IGNORECASE
    ).strip()
    return t


def parse_expiration_command(text: str) -> ExpirationCommand | None:
    """Parse an expiration command from ``text``, or ``None`` when it isn't one.

    Tries the ``set …`` phrasings first (they carry a date), then ``re-extract …``. The single source
    of truth for both the handler and the router branch the integrator adds.
    """
    if not text:
        return None
    for rx in _SET_RES:
        m = rx.search(text)
        if m:
            file = _clean_file(m.group("file"))
            if file:
                return ExpirationCommand(action="set", file=file, date=m.group("date"))
    m = _REEXTRACT_RE.search(text)
    if m:
        file = _clean_file(m.group("file"))
        if file:
            return ExpirationCommand(action="reextract", file=file)
    return None


def matches_expiration_command(text: str) -> bool:
    """True iff ``text`` is a recognized expiration command — the predicate the router branch fires on."""
    return parse_expiration_command(text) is not None


# --------------------------------------------------------------------------- #
# Reply copy
# --------------------------------------------------------------------------- #
USAGE_TEXT = (
    "I can manage NDA expiration dates. Try:\n"
    "• `set expiration of <file> to YYYY-MM-DD` — record a date yourself\n"
    "• `re-extract expiration of <file>` — re-read the signed PDF and update the date"
)
TRACKER_OFF_TEXT = (
    "The expiration tracker (Airtable) isn't set up, so I can't record expiration dates. "
    "Ask an admin to configure it."
)


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #
UpsertFn = Callable[..., Any]
ProcessFn = Callable[..., Any]
SourceResolver = Callable[[Any], Any]


class ExpirationIntent:
    """The ``expiration`` intent handler (PLAN §3.10 trigger c). Callable ``(ctx) -> IntentReply``.

    All side-effecting deps are lazy, injectable seams so the command matrix unit-tests with stubs:

    * ``upsert``          — ``upsert_expiration(file_ref, fields, *, settings, ...)`` (Airtable write);
    * ``process``         — ``process_pdf(pdf_bytes, *, file_ref, display_name, ...)`` (re-extract core);
    * ``source_resolver`` — ``(settings) -> ArchiveSource | None`` (the archive Drive seam, defensive).
    """

    def __init__(
        self,
        *,
        settings=None,
        upsert: UpsertFn | None = None,
        process: ProcessFn | None = None,
        source_resolver: SourceResolver | None = None,
    ) -> None:
        self._settings = settings
        self._upsert = upsert
        self._process = process
        self._source_resolver = source_resolver

    # -- dep resolution (lazy) --------------------------------------------- #
    def _get_settings(self):
        if self._settings is not None:
            return self._settings
        from app.config import get_settings

        return get_settings()

    def _get_upsert(self) -> UpsertFn:
        if self._upsert is not None:
            return self._upsert
        from app.integrations.airtable import upsert_expiration

        return upsert_expiration

    def _get_process(self) -> ProcessFn:
        if self._process is not None:
            return self._process
        from app.expiration.service import process_pdf

        return process_pdf

    def _get_source_resolver(self) -> SourceResolver:
        if self._source_resolver is not None:
            return self._source_resolver
        from app.expiration.jobs import _resolve_archive_source

        return _resolve_archive_source

    # -- entry ------------------------------------------------------------- #
    def __call__(self, ctx: IntentContext) -> IntentReply:
        cmd = parse_expiration_command(ctx.envelope.text)
        if cmd is None:
            return IntentReply(text=USAGE_TEXT)
        if cmd.action == "set":
            return self._handle_set(ctx, cmd)
        return self._handle_reextract(ctx, cmd)

    # -- set (manual override; no LLM) ------------------------------------- #
    def _handle_set(self, ctx: IntentContext, cmd: ExpirationCommand) -> IntentReply:
        from app.integrations.airtable import (
            AirtableError,
            AirtableUnavailable,
            build_expiration_fields,
        )

        settings = self._get_settings()
        upsert = self._get_upsert()
        # The manual override keys the Airtable row on the reference the user named (also its display
        # name). NOTE: if that reference differs from the Drive file id the AUTOMATIC path keys on, the
        # two create separate rows — reconciling them needs the gated Main_Project keying (open_items).
        fields = build_expiration_fields(cmd.file, cmd.date)
        try:
            upsert(cmd.file, fields, settings=settings)
        except AirtableUnavailable:
            log.info(
                "bot.intent.expiration.set.tracker_off",
                event_key=ctx.envelope.event_key,
            )
            return IntentReply(text=TRACKER_OFF_TEXT)
        except AirtableError as exc:
            log.warning(
                "bot.intent.expiration.set.failed",
                event_key=ctx.envelope.event_key,
                error=repr(exc),
            )
            return IntentReply(
                text=(
                    f"Sorry — I couldn't save the expiration date for *{cmd.file}* just now. "
                    "Please try again in a moment."
                )
            )
        log.info(
            "bot.intent.expiration.set.ok",
            event_key=ctx.envelope.event_key,
            file=cmd.file,
            date=cmd.date,
        )
        return IntentReply(
            text=f"Done — recorded *{cmd.file}* as expiring on *{cmd.date}*."
        )

    # -- re-extract (re-run the model over the archived PDF) --------------- #
    def _handle_reextract(
        self, ctx: IntentContext, cmd: ExpirationCommand
    ) -> IntentReply:
        settings = self._get_settings()
        resolve = self._get_source_resolver()
        source = resolve(settings)
        if source is None:
            log.info(
                "bot.intent.expiration.reextract.no_source",
                event_key=ctx.envelope.event_key,
            )
            return IntentReply(
                text=(
                    "I can't reach the signed-NDA archive right now, so I can't re-read "
                    f"*{cmd.file}*. Try again later, or use "
                    "`set expiration of <file> to YYYY-MM-DD`."
                )
            )

        match = _find_in_archive(source, cmd.file)
        if match is None:
            log.info(
                "bot.intent.expiration.reextract.not_found",
                event_key=ctx.envelope.event_key,
                file=cmd.file,
            )
            return IntentReply(
                text=(
                    f"I couldn't find *{cmd.file}* in the signed-NDA archive. Check the name "
                    "and try again."
                )
            )

        try:
            pdf_bytes = source.download(match.file_ref)
        except Exception as exc:  # noqa: BLE001 — a download failure degrades to a friendly reply
            log.warning(
                "bot.intent.expiration.reextract.download_failed",
                event_key=ctx.envelope.event_key,
                file_ref=match.file_ref,
                error=repr(exc),
            )
            return IntentReply(
                text=f"I found *{cmd.file}* but couldn't download it just now. Please try again."
            )

        process = self._get_process()
        outcome = process(
            pdf_bytes,
            file_ref=match.file_ref,
            display_name=match.display_name,
            settings=settings,
        )
        return _reextract_reply(cmd.file, outcome)


def _find_in_archive(source: Any, named: str):
    """Find one archived file whose display name (or file ref) matches ``named``, case-insensitively."""
    want = named.strip().lower()
    for f in source.list_pdfs():
        if f.display_name.strip().lower() == want or f.file_ref.strip().lower() == want:
            return f
    return None


def _reextract_reply(named: str, outcome: Any) -> IntentReply:
    """Turn a :class:`~app.expiration.service.ExpirationOutcome` into a friendly re-extract reply."""
    status = getattr(outcome, "status", "")
    date = getattr(outcome, "date", None)
    if status == "written":
        return IntentReply(
            text=f"Re-read *{named}* — updated its expiration date to *{date}*."
        )
    if status == "airtable_off":
        return IntentReply(
            text=(
                f"I read an expiration date of *{date}* from *{named}*, but the tracker "
                "isn't set up so I couldn't save it."
            )
        )
    if status == "no_date":
        return IntentReply(
            text=(
                f"I re-read *{named}* but still couldn't determine an expiration date from it. "
                "You can set one with `set expiration of <file> to YYYY-MM-DD`."
            )
        )
    if status == "llm_off":
        return IntentReply(
            text="Expiration extraction isn't available right now (the model isn't configured)."
        )
    return IntentReply(
        text=f"Sorry — I hit a problem re-reading *{named}*. Please try again in a moment."
    )


__all__ = [
    "ExpirationIntent",
    "ExpirationCommand",
    "parse_expiration_command",
    "matches_expiration_command",
    "USAGE_TEXT",
    "TRACKER_OFF_TEXT",
]
