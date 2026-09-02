"""DocuSign eSignature integration — JWT-Grant auth + create-and-send envelope (PLAN §3.9).

The bot FILLS the .docx; DocuSign only SIGNS it (a decision, PLAN §2). This module is the one place
that talks to the DocuSign REST API; the ``envelope`` intent handler (``app.bot.intents.envelope``,
another wave) composes signer/routing/cc collection and calls :meth:`DocuSignClient.create_and_send_envelope`,
then persists the attempt via ``app.integrations.models.save_envelope_attempt``.

AUTH — JWT Grant with impersonation
-----------------------------------
We authenticate as a DocuSign *integration* (no interactive OAuth per send) using the JWT Bearer
grant: a short-lived RS256 assertion signed with the integration's RSA private key is exchanged at
``https://{oauth_host}/oauth/token`` for an access token. The assertion carries:

    iss   = integration key (client id)
    sub   = the impersonated API user's id (``docusign_user_id``)
    aud   = the OAuth host WITHOUT scheme (``account-d.docusign.com`` demo / ``account.docusign.com`` prod)
    scope = "signature impersonation"
    iat/exp = now .. now + ttl  (DocuSign caps the assertion lifetime at 1 hour)

**One-time impersonation consent is REQUIRED and is NOT done here.** Before the first successful
token exchange an administrator (or the impersonated user) must grant the integration the
``signature impersonation`` scopes once, by visiting the consent URL:

    https://{oauth_host}/oauth/auth?response_type=code&scope=signature%20impersonation
        &client_id={integration_key}&redirect_uri={registered_redirect_uri}

Until consent is on file DocuSign answers the token exchange with ``400 {"error":"consent_required"}``,
which this module surfaces as a terminal :class:`DocuSignAuthError` whose message points at that step
(see ``docs/CREDENTIALS.md``). The access token is cached in-process until ``expires_in`` minus a skew,
so a burst of sends mints it once.

ERROR TAXONOMY (the caller's intent handler decides the UX)
-----------------------------------------------------------
* 4xx / a DocuSign ``errorCode`` body     -> :class:`DocuSignTerminalError` (``.status_code``,
  ``.error_code`` surfaced) — do not retry; the request itself is wrong.
* 5xx / timeout / connection failure      -> :class:`DocuSignRetryableError` — outage-shaped.
* token exchange 4xx (consent/bad key)    -> :class:`DocuSignAuthError` (a terminal subtype).
* the DOCUSIGN capability is disabled      -> :class:`DocuSignUnavailable` (raised by
  :func:`build_docusign_client`) so the handler can degrade to a friendly reply (capabilities fail
  soft, PLAN §6).

IDEMPOTENCY
-----------
The ported key ``sha1(docx_b64 + "|" + JSON(recipients))[:40]`` (reference §2.9) is sent as the
``X-DocuSign-Idempotency-Key`` header AND returned on the result for persistence, so a duplicate
button click / redelivered event derives the same key and neither DocuSign nor our audit row
double-counts the send.

The httpx transport is injectable (``transport=`` / a ``clock``) so the whole path is exercised with
``httpx.MockTransport`` and zero network in tests.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

import httpx
import jwt

from ..capabilities import DOCUSIGN, CapabilityRegistry, CapabilityState

# --------------------------------------------------------------------------- #
# Ported constants (reference §2.9, §8)
# --------------------------------------------------------------------------- #
#: The execution email subject (reference §2.9). Config-overridable via settings JSON later
#: (PLAN §3.9); a constant for now. NOTE: the n8n ground truth renders this with an EM DASH
#: ("Amperesand — …"); the P3 deliverable spec pins the ASCII hyphen used here. Flagged for human
#: reconciliation (cosmetic; the signer sees it) — see the task open_items.
EMAIL_SUBJECT = "Amperesand - Non-Disclosure Agreement for Execution"

#: The three-way signing-order options collected by the interactivity modal (reference §3.7).
ROUTING_ALL_AT_ONCE = "all_at_once"
ROUTING_AMP_FIRST = "amp_first"
ROUTING_CP_FIRST = "cp_first"
_ROUTINGS = (ROUTING_ALL_AT_ONCE, ROUTING_AMP_FIRST, ROUTING_CP_FIRST)

#: CC placement relative to the signers (reference §2.9): before => routingOrder 1; after =>
#: after the last signer. Default "after".
CC_BEFORE = "before"
CC_AFTER = "after"

_JWT_SCOPES = "signature impersonation"
_JWT_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
#: DocuSign caps the JWT assertion lifetime at 1 hour.
_JWT_TTL_S = 3600
#: Refresh the cached access token this many seconds BEFORE its stated expiry.
_TOKEN_SKEW_S = 60.0

_NAME_SPLIT_RE = re.compile(r"[._-]+")


# --------------------------------------------------------------------------- #
# Error taxonomy
# --------------------------------------------------------------------------- #
class DocuSignError(RuntimeError):
    """Base for any DocuSign integration failure."""


class DocuSignUnavailable(DocuSignError):
    """The DOCUSIGN capability is disabled/misconfigured — the feature is politely off (PLAN §6)."""


class DocuSignRetryableError(DocuSignError):
    """Outage-shaped: 5xx, timeout, or a connection failure — safe to retry with backoff."""


class DocuSignTerminalError(DocuSignError):
    """A definitive rejection (4xx). The request itself is wrong — retrying just re-fails.

    ``status_code`` is the HTTP status; ``error_code`` is DocuSign's ``errorCode`` body field when
    present (surfaced so the intent handler can craft a specific friendly reply).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class DocuSignAuthError(DocuSignTerminalError):
    """The JWT token exchange failed (missing impersonation consent, bad key, wrong user)."""


# --------------------------------------------------------------------------- #
# Pure request construction (no network — golden-testable, reused by the client)
# --------------------------------------------------------------------------- #
def signer_name_from_email(email: str) -> str:
    """Derive a display name from an email local-part (reference §2.9): split ``@``, replace runs of
    ``. _ -`` with spaces, trim. Falls back to the whole address when the local-part is empty."""
    local = email.split("@", 1)[0]
    name = _NAME_SPLIT_RE.sub(" ", local).strip()
    return name or email.strip()


def signer_routing_orders(routing: str, n: int) -> list[int]:
    """Per-signer ``routingOrder`` for ``n`` signers where index 0 is the Amperesand signer and
    indices 1..n-1 are counterparties (reference §2.9, §3.7).

    * ``all_at_once`` — everyone signs in parallel (all order 1).
    * ``amp_first``   — Amperesand signs first, then each counterparty in turn (1, 2, 3, …).
    * ``cp_first``    — counterparties sign first in order (1..n-1), Amperesand signs last (n).

    Unknown values fall back to parallel (the reference's non-sequential default).
    """
    if n <= 0:
        return []
    if routing == ROUTING_AMP_FIRST:
        # amp (idx 0) at 1, then counterparties sequentially after it.
        return list(range(1, n + 1))
    if routing == ROUTING_CP_FIRST:
        # counterparties (idx 1..n-1) at 1..n-1; Amperesand (idx 0) last at n.
        orders = list(range(n))  # [0, 1, 2, …] placeholder
        for i in range(1, n):
            orders[i] = i  # cp1 -> 1, cp2 -> 2, …
        orders[0] = n if n > 1 else 1  # amp last (or 1 when it is the only signer)
        return orders
    # all_at_once and anything unrecognized -> parallel.
    return [1] * n


def cc_routing_order(cc_timing: str, signer_orders: Sequence[int]) -> int:
    """CC ``routingOrder`` (reference §2.9): ``before`` => 1; otherwise one past the last signer."""
    if cc_timing == CC_BEFORE:
        return 1
    return (max(signer_orders) if signer_orders else 0) + 1


def build_recipients(
    signers: Sequence[str],
    routing: str,
    cc: Sequence[str] = (),
    cc_timing: str = CC_AFTER,
) -> dict:
    """The DocuSign ``recipients`` block: signers (index 0 = Amperesand) + carbonCopies.

    ``recipientId`` is a stable 1-based index across signers then CCs (unique per envelope);
    ``routingOrder`` comes from :func:`signer_routing_orders` / :func:`cc_routing_order`. The dict is
    built in a FIXED key order so its ``json.dumps`` is deterministic — it feeds the idempotency key.
    """
    orders = signer_routing_orders(routing, len(signers))
    signer_recipients = [
        {
            "email": email,
            "name": signer_name_from_email(email),
            "recipientId": str(i + 1),
            "routingOrder": str(orders[i]),
        }
        for i, email in enumerate(signers)
    ]
    cc_order = cc_routing_order(cc_timing, orders)
    cc_recipients = [
        {
            "email": email,
            "name": signer_name_from_email(email),
            "recipientId": str(len(signers) + j + 1),
            "routingOrder": str(cc_order),
        }
        for j, email in enumerate(cc)
    ]
    return {"signers": signer_recipients, "carbonCopies": cc_recipients}


def _file_extension(filename: str) -> str:
    ext = PurePosixPath(filename).suffix.lstrip(".").lower()
    return ext or "docx"


def build_envelope_body(
    docx_b64: str,
    filename: str,
    recipients: dict,
    subject: str = EMAIL_SUBJECT,
) -> dict:
    """The full create-and-send envelope body (reference §2.9): document as base64, ``status='sent'``."""
    return {
        "emailSubject": subject,
        "status": "sent",
        "documents": [
            {
                "documentBase64": docx_b64,
                "name": filename or "document.docx",
                "fileExtension": _file_extension(filename),
                "documentId": "1",
            }
        ],
        "recipients": recipients,
    }


def derive_idempotency_key(docx_b64: str, recipients: dict) -> str:
    """The ported idempotency key (reference §2.9): ``sha1(docx_b64 + "|" + JSON(recipients))[:40]``.

    ``JSON(recipients)`` is compact (no whitespace) with keys in construction order — matching
    JavaScript ``JSON.stringify`` — so the same document + recipients always yields the same key.
    A sha1 hexdigest is 40 chars, so the ``[:40]`` slice is the whole digest (kept for rule fidelity).
    """
    material = (
        docx_b64
        + "|"
        + json.dumps(recipients, separators=(",", ":"), ensure_ascii=False)
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:40]


def normalize_routing(routing: str) -> str:
    """Coerce a routing value to one of the three known options (unknown -> parallel default)."""
    return routing if routing in _ROUTINGS else ROUTING_ALL_AT_ONCE


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EnvelopeResult:
    """The outcome of a create-and-send call — what the caller persists + replies from."""

    envelope_id: str
    status: str
    idempotency_key: str
    recipients: dict


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class DocuSignClient:
    """Talks to one DocuSign account: JWT-grant auth (token cached) + create-and-send envelope.

    Construct via :func:`build_docusign_client` (which enforces the capability gate). The httpx
    ``transport`` and ``clock`` are injection seams for tests (``httpx.MockTransport``, a fake clock);
    in production both default to real network + wall-clock.
    """

    def __init__(
        self,
        *,
        base_uri: str,
        oauth_host: str,
        account_id: str,
        integration_key: str,
        user_id: str,
        private_key: str,
        timeout_s: float = 150.0,
        token_skew_s: float = _TOKEN_SKEW_S,
        jwt_ttl_s: int = _JWT_TTL_S,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._base_uri = base_uri.rstrip("/")
        self._oauth_host = oauth_host.strip().rstrip("/")
        self._account_id = account_id
        self._integration_key = integration_key
        self._user_id = user_id
        self._private_key = private_key
        self._token_skew_s = token_skew_s
        self._jwt_ttl_s = jwt_ttl_s
        self._clock = clock
        # One client for both hosts (token exchange + REST); full URLs are passed per call so a
        # MockTransport can route by path in tests.
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_s), transport=transport
        )
        self._access_token: str | None = None
        self._token_expiry: float = 0.0

    # -- lifecycle ---------------------------------------------------------- #
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DocuSignClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- JWT grant auth ----------------------------------------------------- #
    def build_assertion(self, now: float | None = None) -> str:
        """The RS256 JWT bearer assertion (iss/sub/aud/scope/iat/exp). Public for the shape test."""
        issued = int(now if now is not None else self._clock())
        payload = {
            "iss": self._integration_key,
            "sub": self._user_id,
            "aud": self._oauth_host,
            "iat": issued,
            "exp": issued + self._jwt_ttl_s,
            "scope": _JWT_SCOPES,
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    def _mint_token(self) -> None:
        """Exchange a fresh assertion for an access token and cache it until expiry minus skew."""
        assertion = self.build_assertion()
        url = f"https://{self._oauth_host}/oauth/token"
        try:
            resp = self._client.post(
                url,
                data={"grant_type": _JWT_GRANT_TYPE, "assertion": assertion},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.TimeoutException as e:
            raise DocuSignRetryableError(f"docusign token timeout: {e}") from e
        except httpx.TransportError as e:
            raise DocuSignRetryableError(f"docusign token connection error: {e}") from e

        if resp.status_code != 200:
            self._raise_token_error(resp)
        try:
            data = resp.json()
        except ValueError as e:
            raise DocuSignRetryableError(
                "docusign token response was not decodable JSON"
            ) from e
        token = data.get("access_token")
        if not token or not isinstance(token, str):
            raise DocuSignAuthError("docusign token response had no access_token")
        try:
            expires_in = float(data.get("expires_in") or self._jwt_ttl_s)
        except (TypeError, ValueError):
            expires_in = float(self._jwt_ttl_s)
        self._access_token = token
        self._token_expiry = self._clock() + expires_in - self._token_skew_s

    def _raise_token_error(self, resp: httpx.Response) -> None:
        err_code, msg = _parse_docusign_error(resp)
        detail = f"docusign token HTTP {resp.status_code}: {msg}"
        if err_code == "consent_required":
            detail += (
                " — grant one-time impersonation consent for the integration "
                "(see the module docstring / docs/CREDENTIALS.md)."
            )
        # A token exchange never legitimately 5xx-recovers into a usable token within one send; but
        # a 5xx here is still outage-shaped and retryable, whereas 4xx (consent/bad key) is terminal.
        if resp.status_code >= 500:
            raise DocuSignRetryableError(detail)
        raise DocuSignAuthError(
            detail, status_code=resp.status_code, error_code=err_code
        )

    def _token(self) -> str:
        """Return a valid access token, minting one only when the cache is empty/expired."""
        if self._access_token is None or self._clock() >= self._token_expiry:
            self._mint_token()
        assert self._access_token is not None  # _mint_token set it or raised
        return self._access_token

    # -- create + send envelope -------------------------------------------- #
    def create_and_send_envelope(
        self,
        *,
        docx_bytes: bytes,
        filename: str,
        signers: Sequence[str],
        routing: str = ROUTING_ALL_AT_ONCE,
        cc: Sequence[str] = (),
        cc_timing: str = CC_AFTER,
        subject: str = EMAIL_SUBJECT,
    ) -> EnvelopeResult:
        """Build + send an envelope, returning ``{envelope_id, status, idempotency_key, recipients}``.

        ``signers`` is the ordered signer email list (index 0 = the Amperesand signer); ``routing`` is
        one of ``all_at_once | amp_first | cp_first``; ``cc`` / ``cc_timing`` place carbon copies
        before or after the signers. Raises :class:`DocuSignTerminalError` /
        :class:`DocuSignRetryableError` per the taxonomy; the ``idempotency_key`` is both sent as the
        ``X-DocuSign-Idempotency-Key`` header and returned for persistence.
        """
        if not signers:
            raise DocuSignTerminalError("at least one signer email is required")

        routing = normalize_routing(routing)
        docx_b64 = base64.b64encode(docx_bytes).decode("ascii")
        recipients = build_recipients(signers, routing, cc, cc_timing)
        body = build_envelope_body(docx_b64, filename, recipients, subject)
        idempotency_key = derive_idempotency_key(docx_b64, recipients)

        url = f"{self._base_uri}/restapi/v2.1/accounts/{self._account_id}/envelopes"
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
            "X-DocuSign-Idempotency-Key": idempotency_key,
        }
        try:
            resp = self._client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise DocuSignRetryableError(f"docusign envelope timeout: {e}") from e
        except httpx.TransportError as e:
            raise DocuSignRetryableError(
                f"docusign envelope connection error: {e}"
            ) from e

        if resp.status_code >= 300:
            self._raise_envelope_error(resp)

        try:
            data = resp.json()
        except ValueError as e:
            raise DocuSignRetryableError(
                "docusign envelope response was not decodable JSON"
            ) from e
        envelope_id = data.get("envelopeId")
        status = data.get("status")
        # Parity with the reference confirm node: success iff envelopeId present AND status 'sent'.
        if not envelope_id or status != "sent":
            err_code, msg = _parse_docusign_error_data(data)
            raise DocuSignTerminalError(
                f"docusign did not accept the envelope: {msg or data!r}",
                status_code=resp.status_code,
                error_code=err_code,
            )
        return EnvelopeResult(
            envelope_id=str(envelope_id),
            status=str(status),
            idempotency_key=idempotency_key,
            recipients=recipients,
        )

    @staticmethod
    def _raise_envelope_error(resp: httpx.Response) -> None:
        err_code, msg = _parse_docusign_error(resp)
        detail = f"docusign envelope HTTP {resp.status_code}: {msg}"
        # 408/429 and 5xx are outage-shaped (retryable); every other 4xx is a definitive rejection
        # whose DocuSign errorCode is surfaced for the caller's UX.
        if resp.status_code in (408, 429) or resp.status_code >= 500:
            raise DocuSignRetryableError(detail)
        raise DocuSignTerminalError(
            detail, status_code=resp.status_code, error_code=err_code
        )


# --------------------------------------------------------------------------- #
# Error-body parsing
# --------------------------------------------------------------------------- #
def _parse_docusign_error(resp: httpx.Response) -> tuple[str | None, str]:
    """Best-effort ``(errorCode, message)`` from a DocuSign error response body."""
    try:
        data = resp.json()
    except ValueError:
        return None, resp.text[:300]
    if not isinstance(data, dict):
        return None, resp.text[:300]
    return _parse_docusign_error_data(data)


def _parse_docusign_error_data(data: dict) -> tuple[str | None, str]:
    # DocuSign REST errors use ``errorCode``/``message``; the OAuth token endpoint uses
    # ``error``/``error_description``.
    err_code = data.get("errorCode") or data.get("error")
    msg = (
        data.get("message")
        or data.get("error_description")
        or (str(err_code) if err_code else "")
    )
    return (str(err_code) if err_code else None), str(msg)


# --------------------------------------------------------------------------- #
# Capability-gated factory (read-only registry use — PLAN §6)
# --------------------------------------------------------------------------- #
def build_docusign_client(
    settings,
    registry: CapabilityRegistry | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], float] = time.time,
) -> DocuSignClient:
    """Construct a :class:`DocuSignClient` from settings, gating on the DOCUSIGN capability.

    The registry is used READ-ONLY (PLAN §6): a DISABLED/UNHEALTHY DOCUSIGN capability raises
    :class:`DocuSignUnavailable` so the intent handler degrades to a friendly "e-signature isn't set
    up" reply instead of constructing a client with missing credentials. When no registry is passed
    the check is skipped (unit-testing the client directly).
    """
    if registry is not None and registry.state(DOCUSIGN) is not CapabilityState.ENABLED:
        status = registry.get(DOCUSIGN)
        raise DocuSignUnavailable(
            f"docusign capability is {status.state.value}: {status.reason}"
        )
    return DocuSignClient(
        base_uri=settings.docusign_base_uri,
        oauth_host=settings.docusign_oauth_host,
        account_id=settings.docusign_account_id,
        integration_key=settings.docusign_integration_key,
        user_id=settings.docusign_user_id,
        private_key=settings.docusign_private_key,
        timeout_s=settings.provider_timeout_s,
        transport=transport,
        clock=clock,
    )
