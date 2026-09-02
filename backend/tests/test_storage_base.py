"""Archive-storage PROTOCOL-CONFORMANCE suite (PLAN §2) — ZERO network.

The same behavioral contract is exercised against TWO backends, proving they are interchangeable:

* :class:`FakeArchiveStorage` — the in-memory reference backend, directly.
* :class:`GoogleDriveStorage` — driven against ``_FakeDriveServer``, a stateful, Drive-shaped
  ``httpx.MockTransport`` handler (parses the real ``q`` queries + ``multipart/related`` upload the
  client emits). So the real Drive HTTP construction/parsing rides through the same conformance cases.

Drive request-SHAPE goldens (exact URLs / params / body bytes) and the error-taxonomy matrix live in
``test_storage_drive.py``; this file asserts provider-agnostic *behavior*.
"""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import pytest

from app.integrations.storage import (
    FOLDER_MIME_TYPE,
    ArchiveStorage,
    FakeArchiveStorage,
    GoogleDriveStorage,
    StorageEntry,
    StoredFile,
)

_PDF = "application/pdf"


# --------------------------------------------------------------------------- #
# A stateful, Drive-shaped MockTransport server
# --------------------------------------------------------------------------- #
class _FakeDriveServer:
    """In-memory Drive: token endpoint + ``files.list`` ``q`` filtering + multipart upload + download/rename."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self._counter = itertools.count(1)
        self.token_calls = 0

    # -- setup helper (mirrors FakeArchiveStorage.make_folder) -------------- #
    def create_folder(self, name: str, parent: str | None = None) -> str:
        iid = f"srv-folder-{next(self._counter)}"
        self.items[iid] = {
            "id": iid,
            "name": name,
            "parent": parent,
            "mime": FOLDER_MIME_TYPE,
            "content": b"",
            "webViewLink": f"https://drive.test/{iid}",
        }
        return iid

    def _meta(self, item: dict) -> dict:
        return {
            "id": item["id"],
            "name": item["name"],
            "mimeType": item["mime"],
            "webViewLink": item["webViewLink"],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/token"):
            self.token_calls += 1
            return httpx.Response(
                200,
                json={
                    "access_token": "AT-srv",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        if path.endswith("/upload/drive/v3/files"):
            return self._upload(request)
        m = re.match(r"^/drive/v3/files/([^/]+)$", path)
        if m:
            return self._item_op(request, m.group(1))
        if path.endswith("/drive/v3/files"):
            return self._list(request)
        return httpx.Response(404, json={"error": {"code": 404, "message": path}})

    def _upload(self, request: httpx.Request) -> httpx.Response:
        boundary = request.headers["content-type"].split("boundary=")[1]
        meta: dict = {}
        content = b""
        content_mime = "application/octet-stream"
        for seg in request.content.split(b"--" + boundary.encode()):
            seg = seg.strip(b"\r\n")
            if not seg or seg == b"--":
                continue
            head, _, body = seg.partition(b"\r\n\r\n")
            if b"application/json" in head:
                meta = json.loads(body)
            else:
                content = body
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-type:"):
                        content_mime = line.split(b":", 1)[1].strip().decode()
        iid = f"srv-file-{next(self._counter)}"
        parents = meta.get("parents") or [None]
        self.items[iid] = {
            "id": iid,
            "name": meta.get("name", ""),
            "parent": parents[0],
            "mime": content_mime,
            "content": content,
            "webViewLink": f"https://drive.test/{iid}",
        }
        return httpx.Response(200, json=self._meta(self.items[iid]))

    def _item_op(self, request: httpx.Request, file_id: str) -> httpx.Response:
        item = self.items.get(file_id)
        if item is None:
            return httpx.Response(
                404,
                json={
                    "error": {
                        "code": 404,
                        "message": "not found",
                        "errors": [{"reason": "notFound"}],
                    }
                },
            )
        if request.method == "PATCH":
            item["name"] = json.loads(request.content).get("name", item["name"])
            return httpx.Response(200, json=self._meta(item))
        return httpx.Response(200, content=item["content"])  # GET alt=media

    def _list(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("q", "")

        def unescape(s: str) -> str:
            return s.replace("\\'", "'").replace("\\\\", "\\")

        def grab(pattern: str) -> str | None:
            m = re.search(pattern, q)
            return unescape(m.group(1)) if m else None

        want_name = grab(r"name = '((?:[^'\\]|\\.)*)'")
        want_parent = grab(r"'((?:[^'\\]|\\.)*)' in parents")
        want_mime = grab(r"mimeType = '((?:[^'\\]|\\.)*)'")
        files = [
            self._meta(it)
            for it in self.items.values()
            if (want_name is None or it["name"] == want_name)
            and (want_parent is None or it["parent"] == want_parent)
            and (want_mime is None or it["mime"] == want_mime)
        ]
        return httpx.Response(200, json={"files": files})


# --------------------------------------------------------------------------- #
# Parametrized backend harness
# --------------------------------------------------------------------------- #
@dataclass
class _Case:
    storage: ArchiveStorage
    make_folder: Callable[..., str]


@pytest.fixture(params=["fake", "drive"])
def case(request: pytest.FixtureRequest):
    if request.param == "fake":
        st = FakeArchiveStorage()
        yield _Case(
            storage=st,
            make_folder=lambda name, parent=None: st.make_folder(
                name, parent_id=parent
            ),
        )
    else:
        server = _FakeDriveServer()
        st = GoogleDriveStorage(
            client_id="c",
            client_secret="s",
            refresh_token="r",
            transport=httpx.MockTransport(server.handler),
        )
        yield _Case(storage=st, make_folder=server.create_folder)
        st.close()


# --------------------------------------------------------------------------- #
# Conformance
# --------------------------------------------------------------------------- #
def test_backend_satisfies_protocol(case: _Case) -> None:
    assert isinstance(case.storage, ArchiveStorage)


def test_upload_returns_stored_file_and_roundtrips_download(case: _Case) -> None:
    folder = case.make_folder("Signed Company NDAs Cache")
    stored = case.storage.upload(
        name="20260704_A_uNDA_B.pdf",
        content=b"%PDF-1.7 signed",
        content_type=_PDF,
        folder_id=folder,
    )
    assert isinstance(stored, StoredFile)
    assert stored.id
    assert stored.name == "20260704_A_uNDA_B.pdf"
    assert stored.web_link  # both backends surface a link
    assert case.storage.download(stored.id) == b"%PDF-1.7 signed"


def test_find_folder_by_name(case: _Case) -> None:
    fid = case.make_folder("Signed Company NDAs Cache")
    assert case.storage.find_folder_by_name("Signed Company NDAs Cache") == fid
    assert case.storage.find_folder_by_name("No Such Folder") is None


def test_find_folder_by_name_scoped_to_parent(case: _Case) -> None:
    root = case.make_folder("root")
    child = case.make_folder("Envelope_123", parent=root)
    case.make_folder("Envelope_123")  # a same-named folder elsewhere
    assert case.storage.find_folder_by_name("Envelope_123", parent_id=root) == child


def test_list_folder_returns_children(case: _Case) -> None:
    cache = case.make_folder("cache")
    sub = case.make_folder("Envelope_1", parent=cache)
    case.storage.upload(name="a.pdf", content=b"a", content_type=_PDF, folder_id=cache)
    entries = case.storage.list_folder(cache)
    by_name = {e.name: e for e in entries}
    assert set(by_name) == {"Envelope_1", "a.pdf"}
    assert by_name["Envelope_1"].id == sub
    assert by_name["Envelope_1"].mime_type == FOLDER_MIME_TYPE
    assert by_name["a.pdf"].mime_type == _PDF


def test_list_folder_mime_filter(case: _Case) -> None:
    cache = case.make_folder("cache")
    case.make_folder("Envelope_1", parent=cache)
    case.storage.upload(
        name="doc.pdf", content=b"p", content_type=_PDF, folder_id=cache
    )
    folders = case.storage.list_folder(cache, mime_type=FOLDER_MIME_TYPE)
    pdfs = case.storage.list_folder(cache, mime_type=_PDF)
    assert [e.name for e in folders] == ["Envelope_1"]
    assert [e.name for e in pdfs] == ["doc.pdf"]


def test_list_folder_by_name(case: _Case) -> None:
    cache = case.make_folder("Signed Company NDAs Cache")
    case.storage.upload(name="x.pdf", content=b"x", content_type=_PDF, folder_id=cache)
    entries = case.storage.list_folder("Signed Company NDAs Cache", by_name=True)
    assert [e.name for e in entries] == ["x.pdf"]
    # An unknown folder name yields [] (never an error).
    assert case.storage.list_folder("nope", by_name=True) == []


def test_exists_in_folder(case: _Case) -> None:
    cache = case.make_folder("cache")
    case.storage.upload(
        name="dup.pdf", content=b"d", content_type=_PDF, folder_id=cache
    )
    assert case.storage.exists_in_folder(cache, "dup.pdf") is True
    assert case.storage.exists_in_folder(cache, "other.pdf") is False


def test_rename(case: _Case) -> None:
    cache = case.make_folder("cache")
    stored = case.storage.upload(
        name="original.pdf", content=b"o", content_type=_PDF, folder_id=cache
    )
    renamed = case.storage.rename(stored.id, "20260704_A_mNDA_B.pdf")
    assert renamed.id == stored.id
    assert renamed.name == "20260704_A_mNDA_B.pdf"
    # The rename is visible on a subsequent listing.
    assert case.storage.exists_in_folder(cache, "20260704_A_mNDA_B.pdf") is True
    assert case.storage.exists_in_folder(cache, "original.pdf") is False


def test_value_objects_defaults() -> None:
    assert StoredFile(id="1", name="n").web_link is None
    assert StorageEntry(id="1", name="n", mime_type=_PDF).web_link is None
