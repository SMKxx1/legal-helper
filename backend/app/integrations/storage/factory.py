"""Archive-storage provider selection (PLAN §2, §3.10).

:func:`get_archive_storage` returns the configured :class:`ArchiveStorage` provider — Google Drive in
v1, SharePoint later (a stub today). Provider choice is BY CONFIG: the Drive provider is built when the
Google-Drive OAuth trio is present. The SharePoint provider carries no config in v1 (creds pending —
PLAN §2), so it is never selected here; it exists for the seam and its stub is importable directly.

Capability gate (read-only registry use — PLAN §6): a DISABLED/UNHEALTHY ``google_drive`` capability
raises :class:`StorageUnavailable` so the ``archive`` intent / watcher degrade to a friendly reply
instead of constructing a client with missing credentials (capabilities fail soft). When no registry
is passed the gate is skipped (direct unit use) and selection falls back to the raw config check.

Note the split of concerns: the ``google_drive`` *capability* requires the OAuth trio PLUS
``drive_archive_folder_id`` (the watcher's DESTINATION), but the Drive *client* itself only needs the
trio to authenticate — folder ids are a per-call caller concern. So the factory builds the client on
the trio; pass the registry to additionally enforce the full capability contract.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from ...capabilities import GOOGLE_DRIVE, CapabilityRegistry, CapabilityState
from ...config import Settings
from .base import ArchiveStorage, StorageUnavailable
from .drive import GoogleDriveStorage

_DRIVE_AUTH_KEYS = (
    "google_oauth_client_id",
    "google_oauth_client_secret",
    "google_oauth_refresh_token",
)


def get_archive_storage(
    settings: Settings,
    registry: CapabilityRegistry | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], float] = time.time,
) -> ArchiveStorage:
    """Return the configured archive-storage provider, gating on the ``google_drive`` capability.

    Raises :class:`StorageUnavailable` when the capability is disabled/unhealthy (registry given) or no
    provider is configured. ``transport`` / ``clock`` are threaded to the Drive client for tests.
    """
    if (
        registry is not None
        and registry.state(GOOGLE_DRIVE) is not CapabilityState.ENABLED
    ):
        status = registry.get(GOOGLE_DRIVE)
        raise StorageUnavailable(
            f"google_drive capability is {status.state.value}: {status.reason}"
        )
    if settings.is_configured(*_DRIVE_AUTH_KEYS):
        return GoogleDriveStorage(
            client_id=settings.google_oauth_client_id,
            client_secret=settings.google_oauth_client_secret,
            refresh_token=settings.google_oauth_refresh_token,
            timeout_s=settings.provider_timeout_s,
            transport=transport,
            clock=clock,
        )
    raise StorageUnavailable(
        "no archive storage provider configured: set the Google Drive OAuth trio "
        "(GOOGLE_OAUTH_CLIENT_ID/SECRET/REFRESH_TOKEN). The SharePoint provider is a "
        "stub — credentials pending (PLAN §2)."
    )
