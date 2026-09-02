"""The ``template`` intent — deliver a blank NDA .docx, or ask for the missing selectors (PLAN §3.2).

A behavioral port of the n8n ``NDA: Template`` sub-workflow (ground-truth reference §3.2) with the
documented **zero-row fix** (reference §9 gap 7): the old flow fed a 0-row DB result straight into
``Convert to File`` and shipped a broken/empty ``.docx``. Here a missing template is caught and turned
into a friendly, actionable reply naming the exact selector combination — a silent broken document is
strictly worse than a clear "not loaded yet".

The decision is driven entirely by the already-hardened routing fields
(:class:`~app.bot.router.Classification`) — ``jurisdiction`` / ``counterparty_type`` / ``mutuality`` —
so BOTH entry points collapse onto one code path (reference §3.2, task note): the *direct-complete*
turn (the user named all selectors in the first message) and the *email-reply-with-selectors* turn (the
user answered the plain-text ask; the router re-classifies that reply and re-populates the same fields).

Selectors complete (the ported ``Selectors Complete?`` gate — mutuality is required ONLY for an
individual): resolve the current ``empty`` variant .docx from the normalized template schema and reply
with it as ``NDA-template.docx``. Incomplete: on Slack post the ported Block Kit picker
(:func:`app.bot.blockkit.template_picker_blocks`, its ``action_id``s a preserved contract), on email
send the ported plain-text ask listing the accepted values.

The handler is a channel-agnostic ``(IntentContext) -> IntentReply`` (the pipeline delivers what it
returns — text, Block Kit, or the .docx via the reply service's file path). Its only side effect is the
DB read; the DB ``session_factory`` and the ``resolve`` function are injected constructor deps so the
whole selector matrix is unit-tested with a stub resolver and zero network (PLAN house rules).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...telemetry import get_logger
from ..blockkit import TEMPLATE_PICKER_FALLBACK_TEXT, template_picker_blocks
from ..channels.protocol import OutboundAttachment
from . import IntentContext, IntentReply

log = get_logger("nda.bot.intent.template")

#: The delivered blank-template filename (reference §2.10 ``Convert to File`` → ``NDA-template.docx``).
TEMPLATE_FILENAME = "NDA-template.docx"
#: The ported file-reply caption (reference §3.2 ``Template File Reply``).
TEMPLATE_FILE_CAPTION = "Here is your empty NDA template (.docx)."

#: The ported plain-text ask for the email path (reference §3.2 ``Ask Template Selectors``) — lists the
#: accepted values so a reply can be re-classified straight back into complete selectors.
ASK_SELECTORS_TEXT = (
    "To send you a blank NDA template I need a couple of details:\n"
    "• *Jurisdiction* — US or SG\n"
    "• *Counterparty type* — company, service provider, or individual\n"
    "• *Mutuality* (individuals only) — mutual or unilateral\n\n"
    'Reply with those and I\'ll send the template — e.g. "US company template" or '
    '"SG individual mutual template".'
)

#: Display labels for the friendly zero-row message (reference codes → human text).
_CP_DISPLAY = {
    "Company": "Company",
    "ServiceProvider": "Service Provider",
    "Individual": "Individual",
}

#: A DB session read + a template resolver — both injected so tests run with a stub + no network.
SessionFactory = Callable[[], Any]
#: ``resolve(db, jur, cp, mut, variant=...) -> (docx_bytes, template_row)`` (``support_task``'s shape).
TemplateResolver = Callable[..., tuple[bytes, Any]]


def selectors_complete(
    jurisdiction: str, counterparty_type: str, mutuality: str
) -> bool:
    """The ported ``Selectors Complete?`` gate (reference §3.2): jurisdiction AND counterparty present,
    and — ONLY for an individual — mutuality present. Non-individual templates ignore mutuality."""
    if not jurisdiction or not counterparty_type:
        return False
    if counterparty_type == "individual":
        return bool(mutuality)
    return True


def _combo_label(jur: str, cp: str, mut: str) -> str:
    """Human-readable selector combo for the zero-row reply (``US / Individual / Mutual``)."""
    parts = [jur, _CP_DISPLAY.get(cp, cp)]
    if cp == "Individual" and mut and mut != "NotApplicable":
        parts.append(mut)
    return " / ".join(parts)


class TemplateIntent:
    """The ``template`` intent handler (reference §3.2). Callable ``(ctx) -> IntentReply``.

    ``session_factory`` yields a DB session for the template read (defaults to ``app.db.SessionLocal``);
    ``resolve`` fetches the ``empty``-variant .docx bytes (defaults to
    ``app.support_task.resolve_template_docx``). Both are resolved lazily so importing this module stays
    cheap and free of a load-time DB dependency; a test injects a stub resolver and a throwaway factory.
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory | None = None,
        resolve: TemplateResolver | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._resolve = resolve

    # -- dep resolution (lazy, so module import carries no DB/engine load) --
    def _get_session_factory(self) -> SessionFactory:
        if self._session_factory is not None:
            return self._session_factory
        from app.db import SessionLocal

        return SessionLocal

    def _get_resolver(self) -> TemplateResolver:
        if self._resolve is not None:
            return self._resolve
        from app.support_task import resolve_template_docx

        return resolve_template_docx

    def __call__(self, ctx: IntentContext) -> IntentReply:
        c = ctx.classification
        envelope = ctx.envelope
        if selectors_complete(c.jurisdiction, c.counterparty_type, c.mutuality):
            return self._deliver_template(ctx)

        # Incomplete → collect the selectors. Slack gets the interactive picker (its action_ids are the
        # preserved interactivity contract); every other channel gets the ported plain-text ask.
        if envelope.channel == "slack":
            log.info(
                "bot.intent.template.picker",
                event_key=envelope.event_key,
                jurisdiction=c.jurisdiction,
                counterparty_type=c.counterparty_type,
            )
            return IntentReply(
                slack_blocks=tuple(template_picker_blocks()),
                fallback_text=TEMPLATE_PICKER_FALLBACK_TEXT,
            )
        log.info("bot.intent.template.ask", event_key=envelope.event_key)
        return IntentReply(text=ASK_SELECTORS_TEXT)

    def _deliver_template(self, ctx: IntentContext) -> IntentReply:
        """Resolve the ``empty`` variant for the complete selectors and reply with the .docx — with the
        zero-row guard turning a missing template into a friendly, named "not loaded yet" reply."""
        from app.api.errors import EngineError
        from app.support_task.generator import DOCX_MIME, normalize_codes

        c = ctx.classification
        envelope = ctx.envelope
        # Map the hardened bot codes (US/SG, company/service_provider/individual, mutual/unilateral) to
        # the schema's canonical ref codes; the completeness gate above guarantees jur+cp are present and
        # in-set, so this never raises the bad_request path.
        jur, cp, mut = normalize_codes(c.jurisdiction, c.counterparty_type, c.mutuality)
        combo = _combo_label(jur, cp, mut)

        factory = self._get_session_factory()
        resolve = self._get_resolver()
        try:
            with factory() as db:
                docx_bytes, _template = resolve(db, jur, cp, mut, variant="empty")
        except EngineError as exc:
            # ZERO-ROW GUARD (reference §9 gap 7 — the deliberate fix): a missing template version or an
            # unloaded blob is a friendly, actionable reply, never the old broken/empty .docx. Only the
            # not-loaded family maps here; any other EngineError bubbles to the pipeline's friendly error.
            if exc.code in ("template_not_found", "template_blob_missing"):
                log.warning(
                    "bot.intent.template.not_loaded",
                    event_key=envelope.event_key,
                    combo=combo,
                    code=exc.code,
                )
                return IntentReply(
                    text=(
                        f"I don't have the *{combo}* NDA template loaded yet, so I can't send it. "
                        "Ask an admin to upload it, then try again."
                    )
                )
            raise

        if not docx_bytes:
            # Defence in depth: a resolver that returns empty bytes without raising still must not ship a
            # broken document (the exact old bug). Treat it as "not loaded" too.
            log.warning(
                "bot.intent.template.empty_bytes",
                event_key=envelope.event_key,
                combo=combo,
            )
            return IntentReply(
                text=(
                    f"I don't have the *{combo}* NDA template loaded yet, so I can't send it. "
                    "Ask an admin to upload it, then try again."
                )
            )

        log.info(
            "bot.intent.template.delivered",
            event_key=envelope.event_key,
            combo=combo,
            bytes=len(docx_bytes),
        )
        return IntentReply(
            text=TEMPLATE_FILE_CAPTION,
            attachments=(
                OutboundAttachment(
                    filename=TEMPLATE_FILENAME,
                    content=docx_bytes,
                    content_type=DOCX_MIME,
                ),
            ),
        )


__all__ = [
    "TemplateIntent",
    "selectors_complete",
    "TEMPLATE_FILENAME",
    "TEMPLATE_FILE_CAPTION",
    "ASK_SELECTORS_TEXT",
]
