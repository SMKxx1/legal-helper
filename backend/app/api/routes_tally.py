"""Tally webhook intake — ``POST /integrations/tally/webhook`` (PLAN §3.6).

The external replacement for the retired in-house ``/f`` form service. Tally POSTs a signed
``FORM_RESPONSE`` webhook on every submission; this route:

1. verifies the ``tally-signature`` HMAC (fail-closed 401 on mismatch — :func:`app.integrations.tally
   .verify_signature`);
2. dedupes on the Tally submission id (a redelivery is a 200 no-op —
   :func:`app.integrations.models.claim_tally_submission`);
3. maps the fields to the engine token table + routing selectors (:func:`app.integrations.tally
   .map_submission`), and trusts the reply destination ONLY from a bot-issued signed ``channel`` token
   (:func:`app.integrations.tally.verify_routing_token`) — a respondent-supplied raw ``channel`` cannot
   redirect delivery;
4. hands off SYNCHRONOUSLY to :func:`app.bot.flows.generate_completion.run_generation` (before the 200,
   so a crash can't drop a claimed submission) which generates the NDA, delivers it back to the
   requester for review, and offers DocuSign on Slack; the outcome is persisted on the dedup row.

Capability-gated + boot-safe (mirror of ``mount_slack``): with the ``tally`` capability disabled
(``tally_signing_secret`` absent) the route is a clean 503 stub — never a boot error. The body is read
under a size cap BEFORE the HMAC. Everything past the signature check is fail-soft and still returns
200: Tally retries any non-2xx, so a mapping/engine error is logged and swallowed rather than
triggering a redelivery storm.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..capabilities import TALLY, CapabilityState
from ..integrations.tally import (
    SIGNATURE_HEADER,
    map_submission,
    origin_context,
    verify_routing_token,
    verify_signature,
)
from ..telemetry import get_logger

if TYPE_CHECKING:
    from ..capabilities import CapabilityRegistry
    from ..config import Settings

log = get_logger("nda.api.tally")

WEBHOOK_PATH = "/integrations/tally/webhook"

#: Hard cap on the webhook body read BEFORE the signature check — a real Tally payload is a few KB, so
#: this only exists to stop an unauthenticated caller from streaming a huge body into memory (the HMAC
#: must see the whole body). Bounded read below rejects anything larger with a 413.
MAX_WEBHOOK_BYTES = 1024 * 1024  # 1 MiB


def _ok(reason: str) -> JSONResponse:
    return JSONResponse(status_code=200, content={"ok": True, "reason": reason})


def _tally_enabled(settings: Settings, registry: CapabilityRegistry | None) -> bool:
    reg = registry
    if reg is None:
        from ..capabilities import build_registry

        reg = build_registry(settings)
    return reg.state(TALLY) is CapabilityState.ENABLED


def _mark(submission_id: str, status: str) -> None:
    """Best-effort update of the dedup row's outcome (``delivered`` / ``failed`` / ``no_delivery``)."""
    from ..db import SessionLocal
    from ..integrations.models import mark_tally_submission

    try:
        with SessionLocal() as db:
            mark_tally_submission(db, submission_id=submission_id, status=status)
    except Exception as exc:  # noqa: BLE001 — the outcome marker is observability, never load-bearing
        log.warning("tally.mark_failed", submission_id=submission_id, error=repr(exc))


def _run_generation(mapped: Any, origin: dict[str, Any] | None) -> None:
    """Generate + deliver the NDA SYNCHRONOUSLY (before the webhook returns) and record the outcome.

    Run inline rather than as a post-response background task so a crash/restart can't silently drop a
    claimed submission in the gap after the 200. Fully fail-soft: any error is logged + persisted
    (``status='failed'``), never raised — the claimed row stays a durable record for reconciliation."""
    from ..bot.flows import run_generation

    try:
        result = run_generation(
            values=mapped.values,
            jurisdiction=mapped.jurisdiction,
            counterparty_type=mapped.counterparty_type,
            mutuality=mapped.mutuality,
            origin_context=origin or {},
            ref=f"tally:{mapped.submission_id}",
        )
        log.info(
            "tally.generation.done",
            submission_id=mapped.submission_id,
            ok=result.ok,
            reason=result.reason,
            delivered=result.delivered,
        )
        _mark(
            mapped.submission_id,
            "delivered" if result.delivered else (result.reason or "no_delivery")[:32],
        )
    except Exception as exc:  # noqa: BLE001 — generation must never crash the webhook
        log.error(
            "tally.generation.failed",
            submission_id=mapped.submission_id,
            error=repr(exc),
        )
        _mark(mapped.submission_id, "failed")


def register(
    app: FastAPI,
    settings: Settings,
    *,
    registry: CapabilityRegistry | None = None,
) -> None:
    """Mount ``POST /integrations/tally/webhook``, capability-gated and boot-safe.

    Disabled capability → a clean 503 stub (no secret, no processing). Enabled → the verified webhook
    handler. Registered before the catch-all 404 (from ``app.main``)."""
    if not _tally_enabled(settings, registry):
        log.info("tally.mount.disabled")

        async def _disabled() -> JSONResponse:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "tally_disabled",
                        "message": "The Tally intake is not configured.",
                        "details": {},
                    }
                },
            )

        app.add_api_route(WEBHOOK_PATH, _disabled, methods=["POST"])
        return

    async def tally_webhook(request: Request) -> JSONResponse:
        # Bounded read: cap the body BEFORE the HMAC (an unauthenticated caller must not be able to
        # stream an unbounded body into memory). Works for chunked bodies too (no Content-Length trust).
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_WEBHOOK_BYTES:
                log.warning("tally.webhook.too_large", bytes=len(raw))
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "code": "payload_too_large",
                            "message": "Webhook body exceeds the maximum size.",
                            "details": {},
                        }
                    },
                )
        signature = request.headers.get(SIGNATURE_HEADER)
        if not verify_signature(settings.tally_signing_secret, raw, signature):
            log.warning("tally.webhook.bad_signature")
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "code": "invalid_signature",
                        "message": "Tally signature verification failed.",
                        "details": {},
                    }
                },
            )

        # Past the signature gate everything is fail-soft: return 200 so Tally does not retry.
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            log.warning("tally.webhook.bad_json", error=repr(exc))
            return _ok("bad_json")

        try:
            mapped = map_submission(payload)
        except Exception as exc:  # noqa: BLE001 — mapping is best-effort
            log.error("tally.webhook.map_failed", error=repr(exc))
            return _ok("map_failed")

        # Dedup: claim the submission (a redelivery is a no-op).
        from ..db import SessionLocal
        from ..integrations.models import claim_tally_submission

        try:
            with SessionLocal() as db:
                first = claim_tally_submission(
                    db,
                    submission_id=mapped.submission_id,
                    form_id=mapped.form_id,
                    channel=mapped.channel_raw,
                )
        except Exception as exc:  # noqa: BLE001 — dedup must not crash the webhook
            log.error("tally.webhook.dedup_failed", error=repr(exc))
            first = True  # prefer re-processing over silently dropping

        if not first:
            log.info("tally.webhook.duplicate", submission_id=mapped.submission_id)
            return _ok("duplicate")

        # Reply routing is trusted ONLY from a bot-issued signed token (the ``channel`` prefill the
        # generate intent mints). A raw/hand-crafted ``channel`` (someone opening the public form URL
        # with ?channel=… of their choosing) does NOT verify → no auto-delivery to that destination,
        # closing the delivery-redirection hole. The doc is still generated + recorded either way.
        routing = verify_routing_token(
            settings.tally_signing_secret, mapped.channel_raw
        )
        if routing is not None:
            origin = origin_context(routing["channel"], routing["thread_ts"])
        else:
            origin = None
            if mapped.channel_raw:
                log.warning(
                    "tally.webhook.untrusted_routing",
                    submission_id=mapped.submission_id,
                )
        log.info(
            "tally.webhook.accepted",
            submission_id=mapped.submission_id,
            jurisdiction=mapped.jurisdiction,
            counterparty_type=mapped.counterparty_type,
            mutuality=mapped.mutuality,
            has_origin=origin is not None,
            token_count=len(mapped.values),
        )
        _run_generation(mapped, origin)
        return _ok("accepted")

    app.add_api_route(WEBHOOK_PATH, tally_webhook, methods=["POST"])
    log.info("tally.mount.enabled", route=WEBHOOK_PATH)


__all__ = ["register", "WEBHOOK_PATH"]
