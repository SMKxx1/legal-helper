"""GoogleDriveStorage — the v1 archive backend over the Drive v3 REST API with plain httpx (PLAN §2).

The signed-NDA archive lives in Google Drive; this module is the ONE place that talks to the Drive
REST API. It implements :class:`~app.integrations.storage.base.ArchiveStorage` so the ``archive``
intent and the P4 cache-folder watcher never see Drive specifics.

**No SDK dependency.** ``requirements.txt`` is frozen and the config layer explicitly steers builders
to plain httpx against the Google endpoints (see ``app/config.py`` Google-Drive block). Auth is a
stored offline-grant (installed-app) OAuth trio: the refresh token is exchanged at Google's token
endpoint for a short-lived access token, cached in-process until its stated expiry minus a skew, so a
burst of uploads mints it once.

REST surface used
-----------------
* ``POST https://oauth2.googleapis.com/token``                (refresh-token grant)
* ``GET  …/drive/v3/files?q=…``                                (find/list/exists — ``q`` queries)
* ``POST …/upload/drive/v3/files?uploadType=multipart``        (create + content in one multipart/related)
* ``GET  …/drive/v3/files/{id}?alt=media``                     (download bytes)
* ``PATCH …/drive/v3/files/{id}``                              (rename)

``supportsAllDrives`` / ``includeItemsFromAllDrives`` are set on every call so the same code path
works whether the archive lives in My Drive (today: ``My Drive/Amperesand/…/Signed Company NDAs``) or a
shared drive later — harmless for My Drive, required for shared drives.

ERROR TAXONOMY (see :mod:`app.integrations.storage.base`)
---------------------------------------------------------
* 401 / 403 (non-throttle) / a rejected refresh grant -> :class:`StorageAuthError` (mark_unhealthy-worthy)
* 429 / 5xx / a throttle-shaped 403 / timeout / conn   -> :class:`StorageRetryableError`
* any other 4xx                                        -> :class:`StorageTerminalError`

Drive throttles are reported as ``403`` with an ``errors[].reason`` of ``rateLimitExceeded`` /
``userRateLimitExceeded`` / ``dailyLimitExceeded`` — those map to *retryable*, NOT auth, so a transient
throttle never trips the capability UNHEALTHY. This is a deliberate refinement of the "401/403 -> auth"
rule (strictly more correct; documented in the task decisions).

The httpx ``transport`` and ``clock`` are injection seams (``httpx.MockTransport``, a fake clock) so the
whole path — token refresh, multipart upload, ``q`` queries, download, rename, the error matrix — is
exercised with ZERO network in tests.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import httpx

from .base import (
    FOLDER_MIME_TYPE,
    StorageAuthError,
    StorageEntry,
    StorageRetryableError,
    StorageTerminalError,
    StoredFile,
)

# --------------------------------------------------------------------------- #
# Endpoints + constants
# --------------------------------------------------------------------------- #
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
DRIVE_FILES_ENDPOINT = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD_ENDPOINT = "https://www.googleapis.com/upload/drive/v3/files"

#: The metadata fields requested back on every file — enough for :class:`StoredFile` / :class:`StorageEntry`.
_FILE_FIELDS = "id,name,mimeType,webViewLink"

#: Refresh the cached access token this many seconds BEFORE its stated expiry.
_TOKEN_SKEW_S = 60.0
_DEFAULT_TIMEOUT_S = 150.0

#: A distinctive multipart/related boundary. Fixed (not random) so the upload body is golden-testable;
#: a collision with real PDF bytes is astronomically unlikely and Google accepts any boundary token.
_MULTIPART_BOUNDARY = "nda-assistant-drive-boundary-2f7c9a"

#: 403 ``errors[].reason`` values that mean "throttled" (retryable), not "forbidden" (auth). Compared
#: case-folded.
_RATE_LIMIT_REASONS = frozenset(
    {"ratelimitexceeded", "userratelimitexceeded", "dailylimitexceeded"}
)

_GRANT_REFRESH_TOKEN = "refresh_token"


# --------------------------------------------------------------------------- #
# Pure query / body construction (no network — golden-testable, reused by the client)
# --------------------------------------------------------------------------- #
def escape_drive_query_value(value: str) -> str:
    """Escape a string literal for a Drive ``q`` query: backslash then single-quote.

    Drive ``q`` string literals are single-quoted; a folder/file name containing ``'`` (e.g.
    ``O'Brien NDA``) or ``\\`` would otherwise break the query or, worse, alter its meaning. Order
    matters — escape ``\\`` first so the ``'``-escaping backslashes are not themselves doubled.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def folder_name_query(name: str, parent_id: str | None = None) -> str:
    """``q`` to find non-trashed folders named *name* (optionally under *parent_id*)."""
    clauses = [
        f"name = '{escape_drive_query_value(name)}'",
        f"mimeType = '{FOLDER_MIME_TYPE}'",
        "trashed = false",
    ]
    if parent_id:
        clauses.append(f"'{escape_drive_query_value(parent_id)}' in parents")
    return " and ".join(clauses)


def folder_children_query(folder_id: str, mime_type: str | None = None) -> str:
    """``q`` for the non-trashed direct children of *folder_id* (optionally a single *mime_type*)."""
    clauses = [
        f"'{escape_drive_query_value(folder_id)}' in parents",
        "trashed = false",
    ]
    if mime_type:
        clauses.append(f"mimeType = '{escape_drive_query_value(mime_type)}'")
    return " and ".join(clauses)


def name_in_folder_query(folder_id: str, name: str) -> str:
    """``q`` for a non-trashed child named *name* directly under *folder_id* (the duplicate check)."""
    return (
        f"name = '{escape_drive_query_value(name)}' and "
        f"'{escape_drive_query_value(folder_id)}' in parents and trashed = false"
    )


def build_multipart_related(
    metadata: dict,
    content: bytes,
    content_type: str,
    boundary: str = _MULTIPART_BOUNDARY,
) -> tuple[bytes, str]:
    """Build a Drive ``multipart/related`` upload body: a JSON metadata part + the raw content part.

    Returns ``(body_bytes, content_type_header)``. httpx's own ``files=`` produces
    ``multipart/form-data`` — Drive's ``uploadType=multipart`` needs ``multipart/related``, so the body
    is assembled by hand. Metadata JSON is compact (JS-``JSON.stringify`` parity) and the two parts are
    CRLF-delimited by ``--boundary``, closed by ``--boundary--``.
    """
    delim = f"--{boundary}".encode()
    close = f"--{boundary}--".encode()
    meta_json = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
    body = (
        b"\r\n".join(
            [
                delim,
                b"Content-Type: application/json; charset=UTF-8",
                b"",
                meta_json,
                delim,
                f"Content-Type: {content_type}".encode(),
                b"",
                content,
                close,
            ]
        )
        + b"\r\n"
    )
    return body, f"multipart/related; boundary={boundary}"


# --------------------------------------------------------------------------- #
# Error-body parsing
# --------------------------------------------------------------------------- #
def _parse_drive_error(resp: httpx.Response) -> tuple[int | None, str | None, str]:
    """Best-effort ``(code, reason, message)`` from a Drive REST or OAuth error body.

    Drive REST errors are ``{"error": {"code", "message", "errors":[{"reason"}], "status"}}``; the OAuth
    token endpoint uses ``{"error": "invalid_grant", "error_description": …}``. Both are handled.
    """
    try:
        data = resp.json()
    except ValueError:
        return None, None, resp.text[:300]
    if not isinstance(data, dict):
        return None, None, resp.text[:300]
    err = data.get("error")
    if isinstance(err, dict):
        message = str(err.get("message") or "")
        reason: str | None = None
        errors = err.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            reason = errors[0].get("reason")
        reason = reason or err.get("status")
        code = err.get("code") if isinstance(err.get("code"), int) else None
        return code, reason, (message or (reason or ""))
    if isinstance(err, str):  # OAuth token-endpoint shape
        return None, err, str(data.get("error_description") or err)
    return None, None, resp.text[:300]


def _raise_drive_error(resp: httpx.Response) -> None:
    """Map a >=300 Drive REST response to the storage error taxonomy and raise."""
    code, reason, message = _parse_drive_error(resp)
    status = resp.status_code
    detail = f"google drive HTTP {status}: {message}"
    throttle_403 = (
        status == 403
        and reason is not None
        and reason.casefold() in _RATE_LIMIT_REASONS
    )
    if status == 429 or status >= 500 or throttle_403:
        raise StorageRetryableError(detail)
    if status in (401, 403):
        raise StorageAuthError(detail, status_code=status, reason=reason)
    raise StorageTerminalError(detail, status_code=status, reason=reason)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class GoogleDriveStorage:
    """Talks to Google Drive for one OAuth identity: refresh-token auth (cached) + files CRUD.

    Construct directly for unit tests, or via
    :func:`~app.integrations.storage.factory.get_archive_storage` (which enforces the capability gate).
    ``transport`` / ``clock`` are injection seams for tests.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        token_skew_s: float = _TOKEN_SKEW_S,
        boundary: str = _MULTIPART_BOUNDARY,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._token_skew_s = token_skew_s
        self._boundary = boundary
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

    def __enter__(self) -> GoogleDriveStorage:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- OAuth refresh-token grant ----------------------------------------- #
    def _mint_token(self) -> None:
        """Exchange the refresh token for an access token; cache it until expiry minus skew."""
        try:
            resp = self._client.post(
                GOOGLE_TOKEN_ENDPOINT,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                    "grant_type": _GRANT_REFRESH_TOKEN,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.TimeoutException as e:
            raise StorageRetryableError(f"google token timeout: {e}") from e
        except httpx.TransportError as e:
            raise StorageRetryableError(f"google token connection error: {e}") from e

        if resp.status_code != 200:
            self._raise_token_error(resp)
        try:
            data = resp.json()
        except ValueError as e:
            raise StorageRetryableError(
                "google token response was not decodable JSON"
            ) from e
        token = data.get("access_token")
        if not token or not isinstance(token, str):
            raise StorageAuthError("google token response had no access_token")
        try:
            expires_in = float(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600.0
        self._access_token = token
        self._token_expiry = self._clock() + expires_in - self._token_skew_s

    @staticmethod
    def _raise_token_error(resp: httpx.Response) -> None:
        _code, reason, message = _parse_drive_error(resp)
        detail = f"google token HTTP {resp.status_code}: {message}"
        # A 5xx at the token endpoint is outage-shaped; a 4xx (invalid_grant / invalid_client) is a
        # credential problem — mark_unhealthy-worthy, not retryable.
        if resp.status_code >= 500:
            raise StorageRetryableError(detail)
        raise StorageAuthError(detail, status_code=resp.status_code, reason=reason)

    def _token(self) -> str:
        """Return a valid access token, minting one only when the cache is empty/expired."""
        if self._access_token is None or self._clock() >= self._token_expiry:
            self._mint_token()
        assert self._access_token is not None  # _mint_token set it or raised
        return self._access_token

    # -- shared authorized request ----------------------------------------- #
    def _do(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict | None = None,
        content: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token()}"}
        if extra_headers:
            headers.update(extra_headers)
        kwargs: dict[str, object] = {"headers": headers}
        if params is not None:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
        if content is not None:
            kwargs["content"] = content
        try:
            resp = self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
        except httpx.TimeoutException as e:
            raise StorageRetryableError(f"google drive {method} timeout: {e}") from e
        except httpx.TransportError as e:
            raise StorageRetryableError(
                f"google drive {method} connection error: {e}"
            ) from e
        if resp.status_code >= 300:
            _raise_drive_error(resp)
        return resp

    def _query_files(
        self, query: str, *, page_size: int = 1000, max_results: int | None = None
    ) -> list[StorageEntry]:
        """Run a ``files.list`` ``q`` query, following ``nextPageToken`` until exhausted / *max_results*."""
        entries: list[StorageEntry] = []
        page_token: str | None = None
        while True:
            params = {
                "q": query,
                "fields": f"nextPageToken,files({_FILE_FIELDS})",
                "pageSize": str(page_size),
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            resp = self._do("GET", DRIVE_FILES_ENDPOINT, params=params)
            try:
                data = resp.json()
            except ValueError as e:
                raise StorageRetryableError(
                    "google drive list response was not decodable JSON"
                ) from e
            for f in data.get("files") or []:
                entries.append(
                    StorageEntry(
                        id=str(f.get("id", "")),
                        name=str(f.get("name", "")),
                        mime_type=str(f.get("mimeType", "")),
                        web_link=f.get("webViewLink"),
                    )
                )
                if max_results is not None and len(entries) >= max_results:
                    return entries
            page_token = data.get("nextPageToken")
            if not page_token:
                return entries

    # -- ArchiveStorage protocol ------------------------------------------- #
    def find_folder_by_name(
        self, name: str, *, parent_id: str | None = None
    ) -> str | None:
        matches = self._query_files(
            folder_name_query(name, parent_id), page_size=1, max_results=1
        )
        return matches[0].id if matches else None

    def list_folder(
        self, folder: str, *, by_name: bool = False, mime_type: str | None = None
    ) -> list[StorageEntry]:
        folder_id = folder
        if by_name:
            resolved = self.find_folder_by_name(folder)
            if resolved is None:
                return []
            folder_id = resolved
        return self._query_files(folder_children_query(folder_id, mime_type))

    def exists_in_folder(self, folder_id: str, name: str) -> bool:
        return bool(
            self._query_files(
                name_in_folder_query(folder_id, name), page_size=1, max_results=1
            )
        )

    def upload(
        self, *, name: str, content: bytes, content_type: str, folder_id: str
    ) -> StoredFile:
        metadata = {"name": name, "parents": [folder_id]}
        body, ct_header = build_multipart_related(
            metadata, content, content_type, self._boundary
        )
        resp = self._do(
            "POST",
            DRIVE_UPLOAD_ENDPOINT,
            params={
                "uploadType": "multipart",
                "fields": _FILE_FIELDS,
                "supportsAllDrives": "true",
            },
            content=body,
            extra_headers={"Content-Type": ct_header},
        )
        data = _json_or_retryable(resp, "upload")
        file_id = data.get("id")
        if not file_id:
            raise StorageRetryableError("google drive upload response had no file id")
        return StoredFile(
            id=str(file_id),
            name=str(data.get("name") or name),
            web_link=data.get("webViewLink"),
        )

    def download(self, file_id: str) -> bytes:
        resp = self._do(
            "GET",
            f"{DRIVE_FILES_ENDPOINT}/{file_id}",
            params={"alt": "media", "supportsAllDrives": "true"},
        )
        return resp.content

    def rename(self, file_id: str, new_name: str) -> StoredFile:
        resp = self._do(
            "PATCH",
            f"{DRIVE_FILES_ENDPOINT}/{file_id}",
            params={"fields": _FILE_FIELDS, "supportsAllDrives": "true"},
            json_body={"name": new_name},
        )
        data = _json_or_retryable(resp, "rename")
        return StoredFile(
            id=str(data.get("id") or file_id),
            name=str(data.get("name") or new_name),
            web_link=data.get("webViewLink"),
        )


def _json_or_retryable(resp: httpx.Response, op: str) -> dict:
    try:
        data = resp.json()
    except ValueError as e:
        raise StorageRetryableError(
            f"google drive {op} response was not decodable JSON"
        ) from e
    if not isinstance(data, dict):
        raise StorageRetryableError(f"google drive {op} response was not a JSON object")
    return data
