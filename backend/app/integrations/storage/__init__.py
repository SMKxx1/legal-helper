"""Archive-storage providers behind one interface (PLAN §2, §3.10).

The signed-NDA archive is provider-swappable: Google Drive in v1, SharePoint later (a stub today), any
future backend by implementing :class:`ArchiveStorage`. Callers depend on the protocol + the factory,
never on a concrete provider.

Typical use::

    from app.integrations.storage import get_archive_storage, StorageUnavailable
    try:
        storage = get_archive_storage(settings, registry)
    except StorageUnavailable:
        ...  # archival isn't set up — degrade to a friendly reply (capabilities fail soft)

This package carries NO ORM models (unlike ``app.integrations.models``), so importing it is free of the
Base-metadata registration path; it does pull httpx (a core dependency) via the Drive provider.
"""

from __future__ import annotations

from .base import (
    FOLDER_MIME_TYPE,
    PDF_MIME_TYPE,
    ArchiveStorage,
    NotConfigured,
    StorageAuthError,
    StorageEntry,
    StorageError,
    StorageRetryableError,
    StorageTerminalError,
    StorageUnavailable,
    StoredFile,
)
from .drive import GoogleDriveStorage
from .factory import get_archive_storage
from .fake import FakeArchiveStorage
from .sharepoint import SharePointStorage

__all__ = [
    "FOLDER_MIME_TYPE",
    "PDF_MIME_TYPE",
    "ArchiveStorage",
    "FakeArchiveStorage",
    "GoogleDriveStorage",
    "NotConfigured",
    "SharePointStorage",
    "StorageAuthError",
    "StorageEntry",
    "StorageError",
    "StorageRetryableError",
    "StorageTerminalError",
    "StorageUnavailable",
    "StoredFile",
    "get_archive_storage",
]
