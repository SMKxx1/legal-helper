"""Archive-storage factory selection + capability gate + SharePoint stub (PLAN §2, §3.10, §6).

ZERO network: no provider makes a call at construction. The factory picks Google Drive by config and
gates on the ``google_drive`` capability; the SharePoint provider is a protocol-complete stub.
"""

from __future__ import annotations

import pytest

from app.capabilities import GOOGLE_DRIVE, CapabilityState, build_registry
from app.config import Settings
from app.integrations.storage import (
    ArchiveStorage,
    GoogleDriveStorage,
    NotConfigured,
    SharePointStorage,
    StorageUnavailable,
    get_archive_storage,
)

_DRIVE_SETTINGS = dict(
    google_oauth_client_id="cid",
    google_oauth_client_secret="secret",
    google_oauth_refresh_token="refresh",
    drive_archive_folder_id="folder-123",
)


# --------------------------------------------------------------------------- #
# Factory: capability gate (read-only registry use)
# --------------------------------------------------------------------------- #
def test_disabled_capability_raises_unavailable() -> None:
    settings = Settings(_env_file=None)  # no google_* -> capability DISABLED
    registry = build_registry(settings)
    assert registry.state(GOOGLE_DRIVE) is CapabilityState.DISABLED
    with pytest.raises(StorageUnavailable):
        get_archive_storage(settings, registry)


def test_unhealthy_capability_raises_unavailable() -> None:
    settings = Settings(_env_file=None, **_DRIVE_SETTINGS)
    registry = build_registry(settings)
    assert registry.state(GOOGLE_DRIVE) is CapabilityState.ENABLED
    registry.mark_unhealthy(GOOGLE_DRIVE, "drive probe failed")
    with pytest.raises(StorageUnavailable):
        get_archive_storage(settings, registry)


def test_enabled_capability_builds_drive(tmp_path) -> None:
    settings = Settings(_env_file=None, **_DRIVE_SETTINGS)
    registry = build_registry(settings)
    assert registry.state(GOOGLE_DRIVE) is CapabilityState.ENABLED
    storage = get_archive_storage(settings, registry)
    assert isinstance(storage, GoogleDriveStorage)
    assert isinstance(storage, ArchiveStorage)
    storage.close()


# --------------------------------------------------------------------------- #
# Factory: config-driven selection (no registry)
# --------------------------------------------------------------------------- #
def test_builds_drive_on_oauth_trio_without_registry() -> None:
    # The Drive client needs only the OAuth trio to authenticate (folder ids are per-call).
    settings = Settings(
        _env_file=None,
        google_oauth_client_id="cid",
        google_oauth_client_secret="secret",
        google_oauth_refresh_token="refresh",
    )
    storage = get_archive_storage(settings, None)  # gate skipped for direct use
    assert isinstance(storage, GoogleDriveStorage)
    storage.close()


def test_unconfigured_raises_unavailable() -> None:
    settings = Settings(_env_file=None)  # no provider config at all
    with pytest.raises(StorageUnavailable) as ei:
        get_archive_storage(settings, None)
    assert "Google Drive" in str(ei.value)


def test_partial_drive_config_raises_unavailable() -> None:
    # Trio incomplete (missing refresh token) -> not buildable.
    settings = Settings(
        _env_file=None,
        google_oauth_client_id="cid",
        google_oauth_client_secret="secret",
    )
    with pytest.raises(StorageUnavailable):
        get_archive_storage(settings, None)


# --------------------------------------------------------------------------- #
# SharePoint stub (PLAN §2 non-goal)
# --------------------------------------------------------------------------- #
def test_sharepoint_stub_satisfies_protocol() -> None:
    assert isinstance(SharePointStorage(), ArchiveStorage)


def test_not_configured_is_a_storage_unavailable() -> None:
    # A caller catching the fail-soft base degrades gracefully on the stub too.
    assert issubclass(NotConfigured, StorageUnavailable)


def test_sharepoint_every_method_raises_not_configured() -> None:
    sp = SharePointStorage()
    with pytest.raises(NotConfigured):
        sp.find_folder_by_name("x")
    with pytest.raises(NotConfigured):
        sp.list_folder("x")
    with pytest.raises(NotConfigured):
        sp.exists_in_folder("f", "n")
    with pytest.raises(NotConfigured):
        sp.upload(name="n", content=b"c", content_type="application/pdf", folder_id="f")
    with pytest.raises(NotConfigured):
        sp.download("id")
    with pytest.raises(NotConfigured):
        sp.rename("id", "new")
