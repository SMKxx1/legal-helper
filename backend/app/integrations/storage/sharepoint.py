"""SharePointStorage — the STUB archive backend for the stated future destination (PLAN §2).

SharePoint is the company's stated archive destination, but its credentials are pending, so v1 ships a
provider *stub* only (PLAN §1 non-goals, §2 "Google Drive now, SharePoint later behind a
storage-provider interface"). This class exists to prove the :class:`ArchiveStorage` seam is genuinely
provider-swappable: it implements every protocol method, and each raises :class:`NotConfigured` so a
misconfiguration surfaces as a clean, fail-soft "not set up" rather than an AttributeError.

WHEN CREDS LAND — implement each method against Microsoft Graph:

* auth        — client-credentials / on-behalf-of OAuth against
  ``https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`` (scope ``https://graph.microsoft.com/.default``),
  same "mint short-lived token, cache until expiry-skew" shape as :class:`GoogleDriveStorage`.
* find/list   — ``GET /drives/{drive-id}/items/{folder-id}/children`` (folders/files carry an id + name;
  a folder has a ``folder`` facet — map to :data:`FOLDER_MIME_TYPE`).
* exists      — the same children listing filtered by name, or ``GET …/root:/{path}``.
* upload      — ``PUT /drives/{drive-id}/items/{folder-id}:/{name}:/content`` (small files) or an
  upload session for large ones.
* download    — ``GET /drives/{drive-id}/items/{item-id}/content``.
* rename      — ``PATCH /drives/{drive-id}/items/{item-id}`` with ``{"name": new_name}``.

Reuse the same httpx + injectable-transport discipline as ``drive.py``; add the Graph creds to
``app/config.py`` behind a new ``sharepoint`` capability (config layer is frozen — that is the config
agent's change) and teach :func:`~app.integrations.storage.factory.get_archive_storage` to select this
provider when those creds are present. No new pip dep is needed — Graph is plain REST.
"""

from __future__ import annotations

from .base import NotConfigured, StorageEntry, StoredFile

_PENDING = (
    "SharePoint archival is not implemented — Microsoft Graph integration + company "
    "credentials are pending (PLAN §2 non-goal). Configure Google Drive as the archive "
    "provider for now."
)


class SharePointStorage:
    """A protocol-complete placeholder; every operation raises :class:`NotConfigured` (PLAN §2)."""

    def find_folder_by_name(
        self, name: str, *, parent_id: str | None = None
    ) -> str | None:
        raise NotConfigured(_PENDING)

    def list_folder(
        self, folder: str, *, by_name: bool = False, mime_type: str | None = None
    ) -> list[StorageEntry]:
        raise NotConfigured(_PENDING)

    def exists_in_folder(self, folder_id: str, name: str) -> bool:
        raise NotConfigured(_PENDING)

    def upload(
        self, *, name: str, content: bytes, content_type: str, folder_id: str
    ) -> StoredFile:
        raise NotConfigured(_PENDING)

    def download(self, file_id: str) -> bytes:
        raise NotConfigured(_PENDING)

    def rename(self, file_id: str, new_name: str) -> StoredFile:
        raise NotConfigured(_PENDING)
