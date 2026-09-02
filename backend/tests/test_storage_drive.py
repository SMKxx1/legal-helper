"""GoogleDriveStorage goldens + error-taxonomy matrix (PLAN §2, §3.10) — ZERO network.

Pure request/query construction is asserted byte-for-byte; the HTTP paths run through
``httpx.MockTransport`` recorders so token caching, request shapes, and the full error matrix are
exercised without a socket. Behavioral conformance lives in ``test_storage_base.py``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import httpx
import pytest

from app.integrations.storage import (
    GoogleDriveStorage,
    StorageAuthError,
    StorageRetryableError,
    StorageTerminalError,
)
from app.integrations.storage.drive import (
    build_multipart_related,
    escape_drive_query_value,
    folder_children_query,
    folder_name_query,
    name_in_folder_query,
)

_FOLDER_MIME = "application/vnd.google-apps.folder"
_TOKEN_OK = {"access_token": "AT", "expires_in": 3600, "token_type": "Bearer"}


def _storage(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    clock: Callable[[], float] = time.time,
) -> GoogleDriveStorage:
    return GoogleDriveStorage(
        client_id="c",
        client_secret="s",
        refresh_token="r",
        transport=httpx.MockTransport(handler),
        clock=clock,
    )


def _token_then(drive: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json=_TOKEN_OK)
        return drive

    return handler


# --------------------------------------------------------------------------- #
# Pure query construction — goldens
# --------------------------------------------------------------------------- #
def test_folder_name_query_golden() -> None:
    assert folder_name_query("Signed Company NDAs Cache") == (
        "name = 'Signed Company NDAs Cache' and "
        f"mimeType = '{_FOLDER_MIME}' and trashed = false"
    )
    assert folder_name_query("X", "PARENT").endswith("and 'PARENT' in parents")


def test_folder_children_query_golden() -> None:
    assert folder_children_query("FID") == "'FID' in parents and trashed = false"
    assert folder_children_query("FID", "application/pdf") == (
        "'FID' in parents and trashed = false and mimeType = 'application/pdf'"
    )


def test_name_in_folder_query_golden() -> None:
    assert name_in_folder_query("FID", "file.pdf") == (
        "name = 'file.pdf' and 'FID' in parents and trashed = false"
    )


def test_escape_drive_query_value() -> None:
    assert escape_drive_query_value("O'Brien NDA") == "O\\'Brien NDA"
    assert escape_drive_query_value("a\\b") == "a\\\\b"
    # A quote embedded in a name is escaped so it cannot break out of the literal.
    assert "\\'" in folder_name_query("O'Brien")


# --------------------------------------------------------------------------- #
# Multipart/related upload body — golden
# --------------------------------------------------------------------------- #
def test_build_multipart_related_golden() -> None:
    body, content_type = build_multipart_related(
        {"name": "x.pdf", "parents": ["P"]}, b"BYTES", "application/pdf", boundary="BND"
    )
    assert content_type == "multipart/related; boundary=BND"
    assert body == (
        b"--BND\r\n"
        b"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        b'{"name":"x.pdf","parents":["P"]}\r\n'
        b"--BND\r\n"
        b"Content-Type: application/pdf\r\n\r\n"
        b"BYTES\r\n"
        b"--BND--\r\n"
    )


# --------------------------------------------------------------------------- #
# Token: refresh-grant, minted once + cached, re-minted after expiry
# --------------------------------------------------------------------------- #
def test_token_refresh_grant_shape() -> None:
    reqs: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reqs.append(request)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json=_TOKEN_OK)
        return httpx.Response(200, json={"files": []})

    _storage(handler).list_folder("F")
    token_req = next(r for r in reqs if r.url.path.endswith("/token"))
    assert str(token_req.url) == "https://oauth2.googleapis.com/token"
    form = dict(p.split("=", 1) for p in token_req.content.decode().split("&"))
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "r"
    assert form["client_id"] == "c"
    assert form["client_secret"] == "s"


def test_token_minted_once_and_cached() -> None:
    calls = {"token": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls["token"] += 1
            return httpx.Response(200, json=_TOKEN_OK)
        return httpx.Response(200, json={"files": []})

    st = _storage(handler)
    st.list_folder("A")
    st.list_folder("B")
    assert calls["token"] == 1  # cached across calls


def test_token_reminted_after_expiry() -> None:
    calls = {"token": 0}
    now = {"t": 0.0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 100})
        return httpx.Response(200, json={"files": []})

    st = _storage(handler, clock=lambda: now["t"])
    st.list_folder("A")  # mint at t=0; valid until 0 + 100 - 60 = 40
    now["t"] = 41.0
    st.list_folder("A")
    assert calls["token"] == 2


# --------------------------------------------------------------------------- #
# Request shapes
# --------------------------------------------------------------------------- #
def test_upload_request_shape() -> None:
    reqs: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reqs.append(request)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json=_TOKEN_OK)
        return httpx.Response(
            200, json={"id": "F1", "name": "NDA.pdf", "webViewLink": "https://drive/x"}
        )

    result = _storage(handler).upload(
        name="NDA.pdf",
        content=b"%PDF-1.7 payload",
        content_type="application/pdf",
        folder_id="PARENT",
    )
    up = next(r for r in reqs if r.url.path.endswith("/upload/drive/v3/files"))
    assert up.method == "POST"
    assert up.url.params["uploadType"] == "multipart"
    assert up.url.params["fields"] == "id,name,mimeType,webViewLink"
    assert up.url.params["supportsAllDrives"] == "true"
    assert up.headers["Authorization"] == "Bearer AT"
    assert up.headers["content-type"].startswith("multipart/related; boundary=")
    body = up.content
    assert b'"name":"NDA.pdf"' in body
    assert b'"parents":["PARENT"]' in body
    assert b"%PDF-1.7 payload" in body
    assert result.id == "F1"
    assert result.name == "NDA.pdf"
    assert result.web_link == "https://drive/x"


def test_list_request_shape() -> None:
    reqs: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reqs.append(request)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json=_TOKEN_OK)
        return httpx.Response(
            200,
            json={
                "files": [
                    {
                        "id": "a",
                        "name": "x.pdf",
                        "mimeType": "application/pdf",
                        "webViewLink": "L",
                    }
                ]
            },
        )

    entries = _storage(handler).list_folder("FID", mime_type="application/pdf")
    lst = next(
        r for r in reqs if r.method == "GET" and r.url.path.endswith("/drive/v3/files")
    )
    q = lst.url.params["q"]
    assert "'FID' in parents" in q
    assert "trashed = false" in q
    assert "mimeType = 'application/pdf'" in q
    assert lst.url.params["supportsAllDrives"] == "true"
    assert lst.url.params["includeItemsFromAllDrives"] == "true"
    assert "nextPageToken" in lst.url.params["fields"]
    assert entries[0].id == "a"
    assert entries[0].web_link == "L"


def test_find_and_exists_use_q_queries() -> None:
    seen_q: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json=_TOKEN_OK)
        seen_q.append(request.url.params["q"])
        return httpx.Response(200, json={"files": []})

    st = _storage(handler)
    assert st.find_folder_by_name("Cache") is None
    assert st.exists_in_folder("FID", "dup.pdf") is False
    assert f"mimeType = '{_FOLDER_MIME}'" in seen_q[0]  # find scopes to folders
    assert "'FID' in parents" in seen_q[1] and "name = 'dup.pdf'" in seen_q[1]


def test_download_request_shape() -> None:
    reqs: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reqs.append(request)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json=_TOKEN_OK)
        return httpx.Response(200, content=b"RAW-PDF-BYTES")

    data = _storage(handler).download("FILEID")
    dl = next(r for r in reqs if r.url.path.endswith("/drive/v3/files/FILEID"))
    assert dl.method == "GET"
    assert dl.url.params["alt"] == "media"
    assert dl.url.params["supportsAllDrives"] == "true"
    assert data == b"RAW-PDF-BYTES"


def test_rename_request_shape() -> None:
    reqs: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reqs.append(request)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json=_TOKEN_OK)
        name = json.loads(request.content)["name"]
        return httpx.Response(
            200, json={"id": "FILEID", "name": name, "webViewLink": "L"}
        )

    result = _storage(handler).rename("FILEID", "renamed.pdf")
    pr = next(r for r in reqs if r.url.path.endswith("/drive/v3/files/FILEID"))
    assert pr.method == "PATCH"
    assert json.loads(pr.content) == {"name": "renamed.pdf"}
    assert pr.url.params["fields"] == "id,name,mimeType,webViewLink"
    assert result.name == "renamed.pdf"


def test_list_follows_page_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json=_TOKEN_OK)
        if request.url.params.get("pageToken") is None:
            return httpx.Response(
                200,
                json={
                    "files": [
                        {"id": "1", "name": "a.pdf", "mimeType": "application/pdf"}
                    ],
                    "nextPageToken": "P2",
                },
            )
        return httpx.Response(
            200,
            json={
                "files": [{"id": "2", "name": "b.pdf", "mimeType": "application/pdf"}]
            },
        )

    entries = _storage(handler).list_folder("F")
    assert [e.id for e in entries] == ["1", "2"]


# --------------------------------------------------------------------------- #
# Error taxonomy — Drive REST calls
# --------------------------------------------------------------------------- #
def _drive_error(status: int, reason: str | None) -> httpx.Response:
    errors = [{"reason": reason}] if reason else []
    return httpx.Response(
        status, json={"error": {"code": status, "message": "boom", "errors": errors}}
    )


@pytest.mark.parametrize(
    "status,reason", [(401, "authError"), (403, "insufficientPermissions")]
)
def test_401_403_map_to_auth_error(status: int, reason: str) -> None:
    st = _storage(_token_then(_drive_error(status, reason)))
    with pytest.raises(StorageAuthError) as ei:
        st.list_folder("F")
    assert ei.value.status_code == status
    assert ei.value.reason == reason


@pytest.mark.parametrize(
    "reason", ["rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded"]
)
def test_throttle_403_is_retryable_not_auth(reason: str) -> None:
    # Drive reports throttling as 403 + a rate-limit reason — must NOT trip the capability unhealthy.
    st = _storage(_token_then(_drive_error(403, reason)))
    with pytest.raises(StorageRetryableError):
        st.list_folder("F")


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_429_and_5xx_are_retryable(status: int) -> None:
    st = _storage(_token_then(_drive_error(status, "backendError")))
    with pytest.raises(StorageRetryableError):
        st.list_folder("F")


@pytest.mark.parametrize("status", [400, 404])
def test_other_4xx_are_terminal(status: int) -> None:
    st = _storage(_token_then(_drive_error(status, "badRequest")))
    with pytest.raises(StorageTerminalError) as ei:
        st.list_folder("F")
    assert ei.value.status_code == status
    # An auth error is a subtype of terminal — assert this is the plain terminal branch.
    assert not isinstance(ei.value, StorageAuthError)


@pytest.mark.parametrize(
    "exc", [httpx.ReadTimeout("slow"), httpx.ConnectError("no route")]
)
def test_transport_failures_are_retryable(exc: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json=_TOKEN_OK)
        raise exc

    with pytest.raises(StorageRetryableError):
        _storage(handler).list_folder("F")


# --------------------------------------------------------------------------- #
# Error taxonomy — token endpoint
# --------------------------------------------------------------------------- #
def test_token_invalid_grant_is_auth_error() -> None:
    calls = {"drive": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "expired"}
            )
        calls["drive"] += 1
        return httpx.Response(200, json={"files": []})

    with pytest.raises(StorageAuthError) as ei:
        _storage(handler).list_folder("F")
    assert ei.value.reason == "invalid_grant"
    assert calls["drive"] == 0  # never reached the Drive call


def test_token_5xx_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(503, json={"error": "backend_unavailable"})
        return httpx.Response(200, json={"files": []})

    with pytest.raises(StorageRetryableError):
        _storage(handler).list_folder("F")


def test_token_timeout_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(StorageRetryableError):
        _storage(handler).list_folder("F")


def test_upload_missing_id_is_retryable() -> None:
    st = _storage(_token_then(httpx.Response(200, json={"name": "x"})))  # no id
    with pytest.raises(StorageRetryableError):
        st.upload(name="x", content=b"x", content_type="application/pdf", folder_id="F")
