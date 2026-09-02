"""The ``review`` intent — a quick automated NDA review, in-process (PLAN §3.3/§3.4, reference §3.5).

A behavioral port of the review branch of the n8n ``NDA: Envelope Review`` sub-workflow. The allowlist
/ approvals gate has ALREADY run in the pipeline (``app.bot.router.process`` → the fail-closed
``AllowlistGate``) by the time this handler is reached, so this focuses on the work: take the attached
document, extract its text, run the SAME quick review the ``/v1/reviews`` endpoint runs, persist it like
the API does, and reply with the ported severity-grouped summary.

The review runs **through the exact production service path** — it reuses ``routes_v1._extract_text`` /
``routes_v1._run_engine`` (router → per-variant v4 playbook → ``review_service.run_review`` via
``build_engine_gateways``) / ``routes_v1._serialize`` and persists via ``reviews_repo.save_review`` — so
a bot review and an API review are byte-for-byte the same analysis, attributed to a stable bot principal
with ``source_channel`` = ``slack`` | ``email``. Every one of those is an injected constructor dep, so
the whole handler (summary format, persistence, the no-attachment reply, the Slack file fetch) is
unit-tested with fakes and zero network (PLAN house rules). The review is long-running (a multi-call LLM
pass) — fine here: the dispatcher runs it off the Slack ack path inside the claimed ``bot_inbox`` row.

No attachment → the ported "attach the NDA" reply. Thread-doc recovery + confirm chains are **P3**
(reference §2.8 / §3.5) — deliberately not built here.

Attachment bytes are fetched lazily from the channel-specific handle on the
:class:`~app.bot.envelope.AttachmentRef`: an email spool path (read from disk) or a Slack file
(``files.info`` → ``url_private_download`` with the bot token — the injectable :class:`SlackFileFetcher`
seam, stubbed in tests).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...telemetry import get_logger
from ..envelope import AttachmentRef, Envelope
from . import IntentContext, IntentReply

if TYPE_CHECKING:
    from app.config import Settings
    from app.engine.review_service import ReviewResult

log = get_logger("nda.bot.intent.review")

#: The bot's stable engine principal (drives cost attribution / the monthly cap, reference §6). One id
#: for both channels — a bot review is one machine caller regardless of Slack vs email.
REVIEW_ACTOR_ID = "svc:nda-bot"
#: Quick tier only for the bot path (reference §3.5 "Review Engine (quick)"); deep rides the add-in.
REVIEW_MODE = "quick"
#: Findings shown before the overflow line (reference §3.5 "≤25 shown, '…and X more'").
MAX_FINDINGS_SHOWN = 25

#: The ported no-attachment reply (reference §3.5). Thread-doc recovery is P3 — not offered here.
NO_ATTACHMENT_TEXT = (
    "To run a review, attach the NDA you'd like me to check (a `.docx` or `.pdf`) "
    "and send your request again."
)
#: Reply when the attachment can't be fetched (spool gone / Slack download failed).
DOWNLOAD_FAILED_TEXT = "I couldn't open that attachment. Please re-attach the NDA (`.docx` or `.pdf`) and try again."

# Doc-like suffixes used only to PICK which attachment to review; ``_extract_text`` does the
# authoritative validation + friendly rejection.
_DOC_SUFFIXES = frozenset(
    {".docx", ".pdf", ".doc", ".txt", ".md", ".markdown", ".text"}
)

_SEVERITY_ORDER: tuple[str, ...] = ("high", "medium", "low")
_SEVERITY_LABEL = {"high": "High", "medium": "Medium", "low": "Low"}


# --------------------------------------------------------------------------- #
# Injected seams
# --------------------------------------------------------------------------- #
#: Download the bytes for a Slack attachment (``url_private_download`` with the bot token). Injectable.
SlackFileFetcher = Callable[[AttachmentRef], bytes]
#: ``extract_text(filename, data) -> str`` (defaults to ``routes_v1._extract_text``).
TextExtractor = Callable[[str | None, bytes], str]
#: ``run_engine(text) -> ReviewResult`` (defaults to the quick ``routes_v1._run_engine``).
EngineRunner = Callable[[str], "ReviewResult"]
#: ``serialize(review_id, result) -> dict`` (defaults to ``routes_v1._serialize``).
Serializer = Callable[[str, "ReviewResult"], dict]
#: ``save_review(payload, **kw) -> dict`` (defaults to ``reviews_repo.save_review``).
ReviewSaver = Callable[..., Any]


class HttpSlackFileFetcher:
    """The default Slack file fetcher: resolve ``url_private_download`` then GET it with the bot token.

    ``source_ref`` is either the private download URL directly or a Slack file id (then ``files.info``
    resolves the URL first — reference §3.7 ``Retrieve Doc Info`` → ``Download Doc``). Network only, so
    tests inject a fake :data:`SlackFileFetcher` callable instead of constructing this.
    """

    _API_BASE = "https://slack.com/api"

    def __init__(self, token: str, *, timeout_s: float = 30.0) -> None:
        self._token = token or ""
        self._timeout = timeout_s

    def __call__(self, att: AttachmentRef) -> bytes:
        if not self._token:
            raise RuntimeError("no Slack bot token configured for file download")
        ref = (att.source_ref or "").strip()
        if not ref:
            raise ValueError("attachment has no Slack source_ref to download")
        import httpx

        headers = {"Authorization": f"Bearer {self._token}"}
        with httpx.Client(timeout=self._timeout) as client:
            url = (
                ref
                if ref.lower().startswith("http")
                else self._resolve_url(client, headers, ref)
            )
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.content

    def _resolve_url(self, client: Any, headers: dict[str, str], file_id: str) -> str:
        resp = client.get(
            f"{self._API_BASE}/files.info", headers=headers, params={"file": file_id}
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"files.info failed: {data.get('error', 'unknown')}")
        file_obj = data.get("file") or {}
        url = file_obj.get("url_private_download") or file_obj.get("url_private")
        if not url:
            raise RuntimeError("files.info returned no downloadable URL")
        return str(url)


# --------------------------------------------------------------------------- #
# Summary formatting (reference §3.5 "Format Review")
# --------------------------------------------------------------------------- #
def _effective_severity(finding: dict) -> str:
    """Verified severity when the gate ran, else the raw severity (mirrors the engine's own rule)."""
    return str(
        finding.get("verified_severity") or finding.get("severity") or ""
    ).lower()


def _finding_title(finding: dict) -> str:
    """A one-line label for a finding: its title, else the clause heading, else a generic fallback."""
    return str(finding.get("title") or finding.get("clause_heading") or "Issue").strip()


def _degradation_line(components: list[str], bold: Callable[[str], str]) -> str:
    """A caveat line when part of the analysis degraded (#8), phrased so a degradation-driven RED
    is not read as pure legal risk. Empty when nothing degraded."""
    if not components:
        return ""
    names = ", ".join(components)
    return (
        f"{bold('Heads up:')} part of the automated analysis degraded ({names}). "
        "The risk tier may be conservative — an elevated tier here can reflect an incomplete "
        "automated check, not confirmed legal risk. Re-run once the service recovers."
    )


def format_review_summary(payload: dict, *, channel: str) -> str:
    """Render the ported severity-grouped summary (reference §3.5).

    Header = risk tier + adherence score; findings grouped HIGH/MEDIUM/LOW; at most
    :data:`MAX_FINDINGS_SHOWN` listed with an "…and X more." overflow line. ``channel`` selects the
    variant: Slack keeps mrkdwn emphasis + ``•`` bullets, email uses plain text + ``-`` bullets (the
    email sink also strips any residual mrkdwn — this just authors the right surface, reference §2.5).

    The bot runs QUICK (triage) only, so the clean wording is HEDGED (#2): a quick pass locates +
    classifies but does NOT check for DELETED required clauses (that's the deep coverage pass), so a
    clean quick result is not a sign-off. When a coverage pass DID run (deep), the confident wording
    is kept. A degradation caveat (#8) is appended whenever ``analysis_integrity.degraded_components``
    is non-empty, on both the clean and the findings paths.
    """
    slack = channel == "slack"

    def bold(s: str) -> str:
        return f"*{s}*" if slack else s

    bullet = "•" if slack else "-"

    integrity = payload.get("analysis_integrity") or {}
    coverage_ran = bool(integrity.get("coverage_ran"))
    degraded_components = list(integrity.get("degraded_components") or [])
    degradation = _degradation_line(degraded_components, bold)

    tier = str(payload.get("risk_tier") or "").strip() or "unknown"
    score = payload.get("adherence_score")
    score_s = f"{score:.0f}" if isinstance(score, (int, float)) else "n/a"
    header = (
        f"{bold('NDA review complete')} — risk tier {bold(tier)}, "
        f"adherence {bold(score_s + '/100')}."
    )

    findings = [
        f
        for f in (payload.get("findings") or [])
        if _effective_severity(f) in _SEVERITY_ORDER
    ]
    if not findings:
        if coverage_ran:
            clean = "No material issues found."
        else:
            clean = (
                "No obvious issues found in a quick (triage) review. Deleted-clause coverage "
                "is not checked in quick mode — run a deep review for sign-off."
            )
        parts = [header, "", clean]
        if degradation:
            parts += ["", degradation]
        return "\n".join(parts)

    groups = {
        sev: [f for f in findings if _effective_severity(f) == sev]
        for sev in _SEVERITY_ORDER
    }
    total = len(findings)
    lines = [header]
    shown = 0
    for sev in _SEVERITY_ORDER:
        group = groups[sev]
        if not group or shown >= MAX_FINDINGS_SHOWN:
            continue
        lines.append("")
        lines.append(bold(f"{_SEVERITY_LABEL[sev]} ({len(group)})"))
        for finding in group:
            if shown >= MAX_FINDINGS_SHOWN:
                break
            lines.append(f"{bullet} {_finding_title(finding)}")
            shown += 1
    if total > MAX_FINDINGS_SHOWN:
        lines.append("")
        lines.append(f"…and {total - MAX_FINDINGS_SHOWN} more.")
    if degradation:
        lines.append("")
        lines.append(degradation)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The handler
# --------------------------------------------------------------------------- #
class ReviewIntent:
    """The ``review`` intent handler (reference §3.5). Callable ``(ctx) -> IntentReply``.

    Every collaborator is an injected constructor dep (all defaulting to the production ``/v1`` path,
    resolved lazily to keep module import cheap): ``extract_text`` / ``run_engine`` / ``serialize`` /
    ``save_review`` reuse the engine service verbatim, ``slack_fetch`` downloads Slack files, ``settings``
    supplies the bot token / config. Tests pass fakes and drive the whole path with no network + no LLM.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        extract_text: TextExtractor | None = None,
        run_engine: EngineRunner | None = None,
        serialize: Serializer | None = None,
        save_review: ReviewSaver | None = None,
        slack_fetch: SlackFileFetcher | None = None,
    ) -> None:
        self._settings = settings
        self._extract_text = extract_text
        self._run_engine = run_engine
        self._serialize = serialize
        self._save_review = save_review
        self._slack_fetch = slack_fetch

    # -- lazy production defaults ------------------------------------------
    def _get_settings(self) -> Settings:
        if self._settings is not None:
            return self._settings
        from app.config import get_settings

        return get_settings()

    def _get_extract_text(self) -> TextExtractor:
        if self._extract_text is not None:
            return self._extract_text
        from app.api.routes_v1 import _extract_text

        return _extract_text

    def _get_run_engine(self) -> EngineRunner:
        if self._run_engine is not None:
            return self._run_engine
        from app.api.routes_v1 import _run_engine

        def _quick(text: str) -> ReviewResult:
            return _run_engine(
                text, mode=REVIEW_MODE, playbook_version=None, scope="whole"
            )

        return _quick

    def _get_serialize(self) -> Serializer:
        if self._serialize is not None:
            return self._serialize
        from app.api.routes_v1 import _serialize

        return _serialize

    def _get_save_review(self) -> ReviewSaver:
        if self._save_review is not None:
            return self._save_review
        from app.api import reviews_repo

        return reviews_repo.save_review

    def _get_slack_fetch(self) -> SlackFileFetcher:
        if self._slack_fetch is not None:
            return self._slack_fetch
        return HttpSlackFileFetcher(self._get_settings().slack_bot_token)

    # -- resumed entry (approved-request auto-run) -------------------------
    def review_bytes(
        self,
        *,
        filename: str,
        data: bytes,
        origin_envelope: Envelope,
    ) -> IntentReply:
        """Run the review on ALREADY-FETCHED bytes (the approved-request auto-resume path, PLAN §3.4 D).

        Reuses the EXACT extract → run → persist → format path of :meth:`__call__` — no attachment fetch,
        no re-scoring re-implementation. ``origin_envelope`` is the reconstructed origin (its ``channel``
        selects the summary formatting + is recorded on the persisted review). Returns the same friendly
        typed-error replies on an unreadable/oversize document."""
        from app.api.errors import EngineError

        att = AttachmentRef(filename=filename)
        try:
            text = self._get_extract_text()(filename or None, data)
        except EngineError as exc:
            return IntentReply(text=_engine_error_text(exc))
        if not text.strip():
            log.info(
                "bot.intent.review.resume_no_text", event_key=origin_envelope.event_key
            )
            return IntentReply(
                text=(
                    "I couldn't read any text from that document — it may be a scanned "
                    "image. Please send a text-based `.docx` or `.pdf`."
                )
            )
        try:
            result = self._get_run_engine()(text)
        except EngineError as exc:
            return IntentReply(text=_engine_error_text(exc))
        payload = self._persist(origin_envelope, att, data, text, result)
        summary = format_review_summary(payload, channel=origin_envelope.channel)
        log.info(
            "bot.intent.review.resume_done",
            event_key=origin_envelope.event_key,
            review_id=payload.get("review_id", ""),
            risk_tier=payload.get("risk_tier", ""),
        )
        return IntentReply(text=summary)

    # -- entry point -------------------------------------------------------
    def __call__(self, ctx: IntentContext) -> IntentReply:
        envelope = ctx.envelope
        att = _pick_attachment(envelope.attachments)
        if att is None:
            log.info("bot.intent.review.no_attachment", event_key=envelope.event_key)
            return IntentReply(text=NO_ATTACHMENT_TEXT)

        try:
            data = self._load_bytes(envelope, att)
        except Exception as exc:  # noqa: BLE001 — a fetch failure is a friendly reply, not a crash
            log.warning(
                "bot.intent.review.fetch_failed",
                event_key=envelope.event_key,
                filename=att.filename,
                error=repr(exc),
            )
            return IntentReply(text=DOWNLOAD_FAILED_TEXT)

        # Text extraction + the empty-text guard are the SAME as /v1 (reference §3.5 error mapping).
        from app.api.errors import EngineError

        try:
            text = self._get_extract_text()(att.filename or None, data)
        except EngineError as exc:
            return IntentReply(text=_engine_error_text(exc))
        if not text.strip():
            log.info("bot.intent.review.no_text", event_key=envelope.event_key)
            return IntentReply(
                text=(
                    "I couldn't read any text from that document — it may be a scanned "
                    "image. Please send a text-based `.docx` or `.pdf`."
                )
            )

        try:
            result = self._get_run_engine()(text)
        except EngineError as exc:
            # A clean, typed engine error (too large / unprocessable) → friendly reply, no retry.
            return IntentReply(text=_engine_error_text(exc))
        # Any OTHER exception (transient provider/engine failure) propagates: the pipeline degrades to
        # the friendly error reply AND the channel records `failed`, so the worker sweep retries it.

        payload = self._persist(envelope, att, data, text, result)
        summary = format_review_summary(payload, channel=envelope.channel)
        log.info(
            "bot.intent.review.done",
            event_key=envelope.event_key,
            review_id=payload.get("review_id", ""),
            risk_tier=payload.get("risk_tier", ""),
            findings=len(payload.get("findings") or []),
        )
        return IntentReply(text=summary)

    # -- internals ---------------------------------------------------------
    def _load_bytes(self, envelope: Envelope, att: AttachmentRef) -> bytes:
        """Resolve the attachment bytes from its channel-specific handle (email spool path / Slack file)."""
        if envelope.channel == "email":
            ref = (att.source_ref or "").strip()
            if not ref:
                raise ValueError("email attachment was not spooled (no source_ref)")
            return Path(ref).read_bytes()
        # Slack (the only other channel): download via the injectable fetcher seam.
        return self._get_slack_fetch()(att)

    def _persist(
        self,
        envelope: Envelope,
        att: AttachmentRef,
        data: bytes,
        text: str,
        result: ReviewResult,
    ) -> dict:
        """Serialize the review and persist it like ``/v1`` does (best-effort — a persistence failure
        must not cost the user their summary; the paid analysis already ran)."""
        from app.engine.simcache import norm_sha256
        from app.schemas import DEFAULT_ORG_ID

        review_id = uuid.uuid4().hex
        payload = self._get_serialize()(review_id, result)
        try:
            self._get_save_review()(
                payload,
                mode=REVIEW_MODE,
                source_channel=envelope.channel,
                doc_filename=att.filename or "",
                doc_sha256=hashlib.sha256(data).hexdigest(),
                norm_sha256=norm_sha256(text),
                org_id=DEFAULT_ORG_ID,
                actor_user_id=REVIEW_ACTOR_ID,
            )
        except Exception:  # noqa: BLE001 — best-effort persistence (parity with /v1's save)
            log.exception(
                "bot.intent.review.persist_failed",
                event_key=envelope.event_key,
                review_id=review_id,
            )
        return payload


def _pick_attachment(
    attachments: tuple[AttachmentRef, ...],
) -> AttachmentRef | None:
    """The first doc-like attachment (reference §3.5 "Attach Source File"), else the first of any kind,
    else ``None``. ``_extract_text`` still validates + rejects a genuinely unsupported file."""
    if not attachments:
        return None
    for att in attachments:
        suffix = Path(att.filename or "").suffix.lower()
        if suffix in _DOC_SUFFIXES:
            return att
    return attachments[0]


def _engine_error_text(exc: Any) -> str:
    """Map an ``EngineError`` (extraction / engine) to the ported friendly text (reference §3.5)."""
    status = getattr(exc, "status", 0)
    if status == 413:
        return "That document is too large for me to review."
    if status == 415:
        return "I can only review `.docx` or `.pdf` documents."
    if status in (400, 422):
        return (
            "I couldn't process that document — please check it's a text-based "
            "`.docx` or `.pdf` and try again."
        )
    from ..router import ERROR_REPLY_TEXT

    return ERROR_REPLY_TEXT


__all__ = [
    "ReviewIntent",
    "HttpSlackFileFetcher",
    "SlackFileFetcher",
    "format_review_summary",
    "REVIEW_ACTOR_ID",
    "REVIEW_MODE",
    "NO_ATTACHMENT_TEXT",
    "MAX_FINDINGS_SHOWN",
]
