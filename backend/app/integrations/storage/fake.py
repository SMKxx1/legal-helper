"""FakeArchiveStorage — an in-memory reference backend for the :class:`ArchiveStorage` protocol.

A dependency-free, deterministic implementation used by the storage protocol-conformance tests AND
available to downstream P4 waves (the ``archive`` intent and the cache-folder ``watcher``) so they can
drive their logic with ZERO network and no Drive mocking. It is a TEST/DEV double — not a production
provider (nothing is persisted beyond process memory).

Ids are stable, prefixed (``folder-…`` / ``file-…``) counters. :meth:`make_folder` and :meth:`seed_file`
are convenience builders for tests to lay out a folder tree; the six protocol methods behave exactly as
:class:`GoogleDriveStorage`'s do against that tree (same folder/file semantics, same
:data:`FOLDER_MIME_TYPE` filtering, same first-match ``find`` behavior).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from .base import (
    FOLDER_MIME_TYPE,
    StorageEntry,
    StorageTerminalError,
    StoredFile,
)


@dataclass
class _Folder:
    id: str
    name: str
    parent: str | None


@dataclass
class _File:
    id: str
    name: str
    parent: str
    content: bytes
    mime_type: str
    web_link: str


@dataclass
class FakeArchiveStorage:
    """In-memory :class:`ArchiveStorage`. Lay out a tree with :meth:`make_folder` / :meth:`seed_file`."""

    _folders: dict[str, _Folder] = field(default_factory=dict)
    _files: dict[str, _File] = field(default_factory=dict)
    _counter: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    # -- test builders ------------------------------------------------------ #
    def make_folder(self, name: str, *, parent_id: str | None = None) -> str:
        """Create a folder and return its id (a test/setup helper, not part of the protocol)."""
        fid = f"folder-{next(self._counter)}"
        self._folders[fid] = _Folder(id=fid, name=name, parent=parent_id)
        return fid

    def seed_file(
        self,
        name: str,
        *,
        folder_id: str,
        content: bytes = b"",
        mime_type: str = "application/pdf",
    ) -> str:
        """Place a file directly and return its id (a test/setup helper, not part of the protocol)."""
        return self.upload(
            name=name, content=content, content_type=mime_type, folder_id=folder_id
        ).id

    # -- ArchiveStorage protocol ------------------------------------------- #
    def find_folder_by_name(
        self, name: str, *, parent_id: str | None = None
    ) -> str | None:
        for folder in self._folders.values():
            if folder.name == name and (
                parent_id is None or folder.parent == parent_id
            ):
                return folder.id
        return None

    def list_folder(
        self, folder: str, *, by_name: bool = False, mime_type: str | None = None
    ) -> list[StorageEntry]:
        folder_id: str | None = folder
        if by_name:
            folder_id = self.find_folder_by_name(folder)
            if folder_id is None:
                return []
        entries: list[StorageEntry] = []
        if mime_type is None or mime_type == FOLDER_MIME_TYPE:
            entries.extend(
                StorageEntry(
                    id=f.id, name=f.name, mime_type=FOLDER_MIME_TYPE, web_link=None
                )
                for f in self._folders.values()
                if f.parent == folder_id
            )
        if mime_type != FOLDER_MIME_TYPE:
            entries.extend(
                StorageEntry(
                    id=f.id,
                    name=f.name,
                    mime_type=f.mime_type,
                    web_link=f.web_link,
                )
                for f in self._files.values()
                if f.parent == folder_id
                and (mime_type is None or f.mime_type == mime_type)
            )
        return entries

    def exists_in_folder(self, folder_id: str, name: str) -> bool:
        in_files = any(
            f.parent == folder_id and f.name == name for f in self._files.values()
        )
        in_folders = any(
            f.parent == folder_id and f.name == name for f in self._folders.values()
        )
        return in_files or in_folders

    def upload(
        self, *, name: str, content: bytes, content_type: str, folder_id: str
    ) -> StoredFile:
        fid = f"file-{next(self._counter)}"
        web_link = f"https://drive.test/{fid}"
        self._files[fid] = _File(
            id=fid,
            name=name,
            parent=folder_id,
            content=bytes(content),
            mime_type=content_type,
            web_link=web_link,
        )
        return StoredFile(id=fid, name=name, web_link=web_link)

    def download(self, file_id: str) -> bytes:
        file = self._files.get(file_id)
        if file is None:
            raise StorageTerminalError(
                f"file not found: {file_id}", status_code=404, reason="notFound"
            )
        return file.content

    def rename(self, file_id: str, new_name: str) -> StoredFile:
        file = self._files.get(file_id)
        if file is None:
            raise StorageTerminalError(
                f"file not found: {file_id}", status_code=404, reason="notFound"
            )
        file.name = new_name
        return StoredFile(id=file.id, name=new_name, web_link=file.web_link)
