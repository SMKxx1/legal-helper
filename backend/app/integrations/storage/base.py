"""The archive-storage provider interface (PLAN §2, §3.10).

The signed-NDA archive lives behind ONE provider-agnostic protocol so the destination can move from
Google Drive (v1) to SharePoint (later, once company creds land) as a pure config swap — no caller
rewrite. Both the ``archive`` intent (upload a signed NDA into the cache folder) and the P4
cache-folder ``watcher`` (list drops, download for classification, dedupe by name, upload the renamed
copy into the destination folder) talk to THIS interface, never to a concrete SDK.

The abstraction is deliberately Drive/Graph-neutral:

* a **folder** and a **file** are each identified by an opaque string ``id`` the provider mints;
* a **name** is a display name (a folder title / a filename) — never a path;
* ``find_folder_by_name`` resolves a display name to an id (the n8n "resolve cache folder by name"
  step); everything else keys off ids.

Concrete providers: :class:`~app.integrations.storage.drive.GoogleDriveStorage` (v1),
:class:`~app.integrations.storage.sharepoint.SharePointStorage` (stub — PLAN §2 non-goal), and
:class:`~app.integrations.storage.fake.FakeArchiveStorage` (an in-memory reference backend for tests
and local dev). Pick one via :func:`~app.integrations.storage.factory.get_archive_storage`.

ERROR TAXONOMY (the caller decides the UX / retry policy)
---------------------------------------------------------
* :class:`StorageUnavailable`   — the capability is disabled/unhealthy or no provider is configured;
  the archive feature is politely off (capabilities fail soft, PLAN §6). :class:`NotConfigured` (a
  subtype) is what the SharePoint stub raises.
* :class:`StorageAuthError`     — 401 / 403 (non-throttle) / a bad refresh grant: the credentials are
  wrong or lack scope. **mark_unhealthy-worthy** — retrying with the same token cannot help, so the
  caller should ``registry.mark_unhealthy(GOOGLE_DRIVE, …)``.
* :class:`StorageRetryableError`— 429 / 5xx / a throttle-shaped 403 / timeout / connection failure:
  outage-shaped, safe to retry with backoff.
* :class:`StorageTerminalError` — any other 4xx (e.g. 404 / 400): the request itself is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: Google Drive's folder MIME type — the provider-neutral "this entry is a folder" marker surfaced on
#: :class:`StorageEntry.mime_type`. Defined here (not just in ``drive``) so the fake backend and any
#: caller can filter folders vs files without importing the concrete provider.
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

#: The PDF content type — the provider-neutral filter the watcher passes to :meth:`ArchiveStorage.list_folder`
#: to enumerate signed-NDA drops, and the ``content_type`` the archive intent + watcher upload with. Kept
#: here (beside :data:`FOLDER_MIME_TYPE`) so callers never reach into a concrete provider for it.
PDF_MIME_TYPE = "application/pdf"


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StoredFile:
    """The result of an ``upload`` / ``rename`` — what the caller records + replies from.

    ``web_link`` is the provider's human-openable URL (Drive ``webViewLink``) when available; a
    provider that cannot cheaply produce one leaves it ``None``.
    """

    id: str
    name: str
    web_link: str | None = None


@dataclass(frozen=True)
class StorageEntry:
    """One child of a folder returned by ``list_folder`` (a subfolder or a file).

    ``mime_type`` distinguishes folders (:data:`FOLDER_MIME_TYPE`) from files (e.g.
    ``application/pdf``) so the watcher can walk envelope subfolders and PDF drops with one call each.
    """

    id: str
    name: str
    mime_type: str
    web_link: str | None = None


# --------------------------------------------------------------------------- #
# Error taxonomy
# --------------------------------------------------------------------------- #
class StorageError(RuntimeError):
    """Base for any archive-storage failure."""


class StorageUnavailable(StorageError):
    """The storage capability is disabled/unhealthy or unconfigured — the feature is politely off.

    Raised by :func:`~app.integrations.storage.factory.get_archive_storage` when the ``google_drive``
    capability is not ENABLED (or no provider's config is present) so the intent handler degrades to a
    friendly "archival isn't set up" reply instead of building a client with missing credentials
    (capabilities fail soft, PLAN §6).
    """


class NotConfigured(StorageUnavailable):
    """A selected provider is not usable yet (creds/implementation pending).

    Raised by the SharePoint stub (PLAN §2: Graph API + company creds pending). A subtype of
    :class:`StorageUnavailable` so a caller catching the fail-soft base also degrades gracefully here.
    """


class StorageTerminalError(StorageError):
    """A definitive rejection (a 4xx other than auth). The request is wrong — retrying just re-fails.

    ``status_code`` is the HTTP status; ``reason`` is the provider's machine reason (Drive's
    ``errors[].reason`` / ``status``) when present, surfaced for the caller's UX.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


class StorageAuthError(StorageTerminalError):
    """401 / non-throttle 403 / a rejected refresh grant — credentials are wrong or lack scope.

    **mark_unhealthy-worthy**: the caller should transition the ``google_drive`` capability to
    UNHEALTHY (PLAN §6) rather than retry, since the same token will keep failing.
    """


class StorageRetryableError(StorageError):
    """Outage-shaped: 429, 5xx, a throttle-shaped 403, a timeout, or a connection failure.

    Safe to retry with backoff. Distinct from :class:`StorageAuthError` so a transient Drive throttle
    (which Google reports as ``403 rateLimitExceeded``) never trips a capability into UNHEALTHY.
    """


# --------------------------------------------------------------------------- #
# The provider protocol
# --------------------------------------------------------------------------- #
@runtime_checkable
class ArchiveStorage(Protocol):
    """The provider-agnostic archive interface implemented by every storage backend (PLAN §2).

    ``@runtime_checkable`` so a conformance test can assert ``isinstance(provider, ArchiveStorage)``
    (structural: method presence, not signatures).
    """

    def find_folder_by_name(
        self, name: str, *, parent_id: str | None = None
    ) -> str | None:
        """Resolve a folder's display *name* to its opaque id, or ``None`` if no such folder exists.

        Scoped to *parent_id*'s direct children when given (else searched provider-wide). When several
        folders share the name the first match is returned (matching the n8n ``limit 1`` resolve step).
        """
        ...

    def list_folder(
        self, folder: str, *, by_name: bool = False, mime_type: str | None = None
    ) -> list[StorageEntry]:
        """List the direct children of a folder (subfolders + files).

        *folder* is a folder id by default; pass ``by_name=True`` to give a folder display name
        instead (resolved via :meth:`find_folder_by_name` first — an unknown name yields ``[]``).
        *mime_type* filters to a single type (e.g. :data:`FOLDER_MIME_TYPE` for subfolders,
        ``"application/pdf"`` for PDF drops); ``None`` returns everything.
        """
        ...

    def exists_in_folder(self, folder_id: str, name: str) -> bool:
        """Whether a (non-trashed) child named *name* already exists directly under *folder_id*.

        The watcher's destination-folder duplicate check (PLAN §3.10) before uploading a renamed copy.
        """
        ...

    def upload(
        self, *, name: str, content: bytes, content_type: str, folder_id: str
    ) -> StoredFile:
        """Upload *content* as a new file named *name* into *folder_id*; return the created file."""
        ...

    def download(self, file_id: str) -> bytes:
        """Return the raw bytes of *file_id* (for PDF-text classification in the watcher)."""
        ...

    def rename(self, file_id: str, new_name: str) -> StoredFile:
        """Rename *file_id* to *new_name* in place; return the updated file."""
        ...
