"""Airtable expiration tracker — capability-gated upsert of extracted NDA expiration dates (PLAN §3.10, §6).

The one place that talks to the Airtable REST API (httpx + a PAT bearer; no SDK dep — requirements.txt
is frozen). It writes MINIMAL fields per PLAN §6 — a file reference, a display name, and the extracted
expiration date — and NOTHING else (no full party payloads; the base's DPA is noted in CREDENTIALS.md).

UPSERT, not blind insert
------------------------
Writes go through Airtable's native upsert: ``PATCH /v0/{base}/{table}`` with
``performUpsert.fieldsToMergeOn`` set to the file-reference field, so re-processing the same NDA (a
re-drive of the archive hook, a nightly sweep pass, a manual re-extract) UPDATES the one row instead
of piling up duplicates. ``typecast: true`` lets Airtable coerce the ISO date string into a Date field.

Field names
-----------
Airtable field names are user-defined on the base, so the three the writer targets are module
constants (:data:`FIELD_FILE_REF`, :data:`FIELD_DISPLAY_NAME`, :data:`FIELD_EXPIRATION_DATE`). If the
provisioned base uses different column names, change them HERE (one place) — flagged for the operator
in the task open_items / CREDENTIALS.md.

Capability gate (fail soft, PLAN §6)
------------------------------------
:func:`build_airtable_client` / :func:`upsert_expiration` raise :class:`AirtableUnavailable` when the
``airtable`` capability is disabled (PAT / base id / table missing). Callers (the extraction service,
the manual override command) catch it and degrade: extraction still runs and logs, the write is a
clean no-op. Per-request failures map to a retryable/terminal taxonomy like the other integrations.

The httpx ``transport`` is an injection seam so every path runs on ``httpx.MockTransport`` with zero
network in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

import httpx

from ..capabilities import AIRTABLE, CapabilityRegistry, CapabilityState
from ..telemetry import get_logger

log = get_logger("nda.integrations.airtable")

#: Airtable REST base. Full ``/v0/{baseId}/{tableIdOrName}`` is composed per client.
AIRTABLE_API_BASE = "https://api.airtable.com/v0"

# --------------------------------------------------------------------------- #
# Field names (change here if the provisioned base uses different columns)
# --------------------------------------------------------------------------- #
#: The MERGE key — a stable file reference (the archive Drive file id). Must be unique-per-NDA in the
#: base for upsert to target one row. This is the field ``fieldsToMergeOn`` points at.
FIELD_FILE_REF = "File Id"
#: The human-friendly NDA name (the archive display/rename), for people reading the tracker.
FIELD_DISPLAY_NAME = "Name"
#: The extracted agreement expiration date (ISO ``YYYY-MM-DD``; Airtable coerces to its Date type).
FIELD_EXPIRATION_DATE = "Expiration Date"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class AirtableError(RuntimeError):
    """Base for any Airtable integration failure."""


class AirtableUnavailable(AirtableError):
    """The ``airtable`` capability is disabled/misconfigured — the tracker is politely off (PLAN §6)."""


class AirtableRetryableError(AirtableError):
    """Outage-shaped: 429 / 5xx / timeout / connection failure — safe to retry later."""


class AirtableTerminalError(AirtableError):
    """A definitive rejection (4xx other than 429). The request itself is wrong — do not retry.

    ``status_code`` is the HTTP status; ``error_type`` is Airtable's ``error.type`` body field when
    present (e.g. ``INVALID_REQUEST_UNKNOWN`` for an unknown field name — a schema mismatch).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


# --------------------------------------------------------------------------- #
# Field helper
# --------------------------------------------------------------------------- #
def build_expiration_fields(
    display_name: str, expiration_date: str, *, extra: dict | None = None
) -> dict:
    """The MINIMAL Airtable field set for one NDA (PLAN §6): display name + expiration date.

    The merge key (:data:`FIELD_FILE_REF`) is added by :meth:`AirtableClient.upsert_expiration` from
    the ``file_ref`` argument, so it is NOT included here. ``extra`` allows a caller to add further
    known columns (kept out of the default so the payload stays minimal by construction).
    """
    fields: dict = {
        FIELD_DISPLAY_NAME: display_name,
        FIELD_EXPIRATION_DATE: expiration_date,
    }
    if extra:
        fields.update(extra)
    return fields


# --------------------------------------------------------------------------- #
# Tracked record (for the sweep's "already extracted?" check)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TrackedRecord:
    """One Airtable row as the sweep sees it: its file reference and whether a date is already set."""

    file_ref: str
    expiration_date: str  # "" when the Date cell is empty (a straggler to (re)extract)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class AirtableClient:
    """Talks to one Airtable base+table: upsert expiration rows + list tracked file references.

    Construct via :func:`build_airtable_client` (which enforces the capability gate). ``transport`` is
    an injection seam for tests (``httpx.MockTransport``); in production it defaults to real network.
    """

    def __init__(
        self,
        *,
        pat: str,
        base_id: str,
        table: str,
        base_url: str = AIRTABLE_API_BASE,
        timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # Table name/id is path-encoded (a human-readable table name may contain spaces).
        self._url = f"{base_url.rstrip('/')}/{base_id}/{quote(table, safe='')}"
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {pat}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(timeout_s),
            transport=transport,
        )

    # -- lifecycle ---------------------------------------------------------- #
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AirtableClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- writes ------------------------------------------------------------- #
    def upsert_expiration(self, file_ref: str, fields: dict) -> dict:
        """Upsert one NDA's expiration row, keyed on :data:`FIELD_FILE_REF` == ``file_ref``.

        ``fields`` carries the display name + expiration date (see :func:`build_expiration_fields`); the
        merge-key field is injected from ``file_ref`` here so the caller never has to know the column
        name twice. Returns the created/updated record dict. Re-processing the same NDA updates the one
        matching row instead of inserting a duplicate.
        """
        payload = dict(fields)
        payload[FIELD_FILE_REF] = file_ref
        body = {
            "performUpsert": {"fieldsToMergeOn": [FIELD_FILE_REF]},
            "records": [{"fields": payload}],
            "typecast": True,  # coerce the ISO date string into the base's Date field
        }
        data = self._request("PATCH", self._url, json=body)
        records = data.get("records") or []
        record = records[0] if records else {}
        log.info(
            "airtable.upserted",
            file_ref=file_ref,
            record_id=record.get("id"),
            has_date=bool(payload.get(FIELD_EXPIRATION_DATE)),
        )
        return record

    # -- reads -------------------------------------------------------------- #
    def list_tracked(self) -> list[TrackedRecord]:
        """List every row's ``(file_ref, expiration_date)`` — the sweep's dedup source.

        Selects only the two relevant fields (minimizing payload) and follows Airtable's ``offset``
        pagination to completion. Rows with no file reference are skipped (they can't be a dedup key).
        """
        out: list[TrackedRecord] = []
        params: dict = {
            "fields[]": [FIELD_FILE_REF, FIELD_EXPIRATION_DATE],
            "pageSize": 100,
        }
        offset: str | None = None
        while True:
            q = dict(params)
            if offset:
                q["offset"] = offset
            data = self._request("GET", self._url, params=q)
            for rec in data.get("records") or []:
                f = rec.get("fields") or {}
                ref = f.get(FIELD_FILE_REF)
                if not ref:
                    continue
                out.append(
                    TrackedRecord(
                        file_ref=str(ref),
                        expiration_date=str(f.get(FIELD_EXPIRATION_DATE) or ""),
                    )
                )
            offset = data.get("offset")
            if not offset:
                return out

    # -- HTTP + taxonomy ---------------------------------------------------- #
    def _request(self, method: str, url: str, **kw) -> dict:
        try:
            resp = self._client.request(method, url, **kw)
        except httpx.TimeoutException as e:
            raise AirtableRetryableError(f"airtable timeout: {e}") from e
        except httpx.TransportError as e:
            raise AirtableRetryableError(f"airtable connection error: {e}") from e
        if resp.status_code >= 400:
            self._raise_status(resp)
        try:
            data = resp.json()
        except ValueError as e:
            raise AirtableTerminalError(
                f"airtable returned an undecodable body: {e}",
                status_code=resp.status_code,
            ) from e
        if not isinstance(data, dict):
            raise AirtableTerminalError(
                "airtable body was not a JSON object", status_code=resp.status_code
            )
        return data

    @staticmethod
    def _raise_status(resp: httpx.Response) -> None:
        """Map a >=400 status onto the taxonomy. 429/5xx are outage-shaped (retryable); every other
        4xx answers definitively (terminal) — 401/403 bad PAT, 404 bad base/table, 422 unknown field."""
        etype = None
        msg = resp.text[:200]
        try:
            err = (resp.json() or {}).get("error")
            if isinstance(err, dict):
                etype = err.get("type")
                msg = err.get("message") or etype or msg
            elif isinstance(err, str):
                msg = err
        except ValueError:
            pass
        detail = f"airtable HTTP {resp.status_code}: {msg}"
        if resp.status_code == 429 or resp.status_code >= 500:
            raise AirtableRetryableError(detail)
        raise AirtableTerminalError(
            detail, status_code=resp.status_code, error_type=etype
        )


# --------------------------------------------------------------------------- #
# Capability-gated factory + convenience
# --------------------------------------------------------------------------- #
def build_airtable_client(
    settings,
    registry: CapabilityRegistry | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> AirtableClient:
    """Construct an :class:`AirtableClient` from settings, gating on the AIRTABLE capability.

    The registry is used READ-ONLY (PLAN §6): a disabled/unhealthy AIRTABLE capability raises
    :class:`AirtableUnavailable` so callers degrade to a no-op instead of building a client with
    missing credentials. When no registry is passed the config presence is checked directly (so the
    gate holds even where a registry isn't threaded through).
    """
    if registry is not None:
        if registry.state(AIRTABLE) is not CapabilityState.ENABLED:
            status = registry.get(AIRTABLE)
            raise AirtableUnavailable(
                f"airtable capability is {status.state.value}: {status.reason}"
            )
    elif not settings.is_configured(
        "airtable_pat", "airtable_base_id", "airtable_table"
    ):
        missing = settings.missing_config(
            "airtable_pat", "airtable_base_id", "airtable_table"
        )
        raise AirtableUnavailable(
            f"airtable capability is disabled: missing config {', '.join(missing)}"
        )
    return AirtableClient(
        pat=settings.airtable_pat,
        base_id=settings.airtable_base_id,
        table=settings.airtable_table,
        transport=transport,
    )


def upsert_expiration(
    file_ref: str,
    fields: dict,
    *,
    settings=None,
    registry: CapabilityRegistry | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """Upsert one NDA's expiration row — the task's named entry point (PLAN §3.10).

    Builds a capability-gated client (raising :class:`AirtableUnavailable` when the tracker is off) and
    upserts ``fields`` keyed on ``file_ref``. Callers that must degrade catch :class:`AirtableUnavailable`
    (feature off) and :class:`AirtableError` (a transient/terminal write failure).
    """
    from ..config import get_settings

    settings = settings or get_settings()
    with build_airtable_client(settings, registry, transport=transport) as client:
        return client.upsert_expiration(file_ref, fields)


__all__ = [
    "AIRTABLE_API_BASE",
    "FIELD_FILE_REF",
    "FIELD_DISPLAY_NAME",
    "FIELD_EXPIRATION_DATE",
    "AirtableError",
    "AirtableUnavailable",
    "AirtableRetryableError",
    "AirtableTerminalError",
    "TrackedRecord",
    "AirtableClient",
    "build_expiration_fields",
    "build_airtable_client",
    "upsert_expiration",
]
