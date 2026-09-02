"""Archive-time trigger — extract_on_archived forwarding + registration (PLAN §3.10 trigger a).

Integrates with the archive agent's real ``app.archive.hooks`` registry: the subscriber reads an
``ArchivedFile`` and forwards to ``process_pdf``, never raises, and ``register_archive_hook`` subscribes
into the shared registry idempotently.
"""

from __future__ import annotations

import pytest

from app.expiration import hooks
from app.expiration.hooks import extract_on_archived, register_archive_hook
from app.expiration.service import ExpirationOutcome


@pytest.fixture
def archived_file():
    from app.archive.hooks import ArchivedFile

    return ArchivedFile(
        file_name="20270101_Amp_mNDA_Acme.pdf",
        file_id="drive-dest-9",
        folder_id="archive-folder",
        pdf_bytes=b"%PDF bytes",
        source_file_id="cache-src-1",
        envelope_folder="Envelope_abc",
        classification={"issuer": "Amperesand", "nda_type": "mNDA"},
    )


@pytest.fixture(autouse=True)
def _clear_archive_hooks():
    from app.archive.hooks import clear_hooks

    clear_hooks()
    yield
    clear_hooks()


def test_extract_on_archived_forwards_destination_id_and_name(
    monkeypatch, archived_file
) -> None:
    seen: dict = {}

    def fake_process(pdf_bytes, *, file_ref, display_name, settings=None):
        seen.update(pdf=pdf_bytes, file_ref=file_ref, display_name=display_name)
        return ExpirationOutcome(
            file_ref=file_ref, date="2027-01-01", upserted=True, status="written"
        )

    monkeypatch.setattr(hooks, "process_pdf", fake_process)
    out = extract_on_archived(archived_file)
    assert seen == {
        "pdf": b"%PDF bytes",
        # keyed on the DESTINATION file id (same key the sweep uses -> one row, no duplicates)
        "file_ref": "drive-dest-9",
        "display_name": "20270101_Amp_mNDA_Acme.pdf",
    }
    assert out.status == "written"


def test_extract_on_archived_falls_back_to_source_id_then_name(monkeypatch) -> None:
    from app.archive.hooks import ArchivedFile

    seen: dict = {}
    monkeypatch.setattr(
        hooks,
        "process_pdf",
        lambda pdf, *, file_ref, display_name, settings=None: (
            seen.setdefault("ref", file_ref)
            or ExpirationOutcome(
                file_ref=file_ref, date=None, upserted=False, status="no_date"
            )
        ),
    )
    # No destination file_id -> falls back to source_file_id.
    extract_on_archived(ArchivedFile(file_name="n.pdf", source_file_id="cache-src-1"))
    assert seen["ref"] == "cache-src-1"


def test_extract_on_archived_never_raises(monkeypatch, archived_file) -> None:
    def boom(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(hooks, "process_pdf", boom)
    out = extract_on_archived(archived_file)
    assert out.status == "hook_error"
    assert out.upserted is False


def test_register_archive_hook_subscribes_into_registry() -> None:
    from app.archive.hooks import registered_count

    assert registered_count() == 0
    assert register_archive_hook() is True
    assert registered_count() == 1
    # Idempotent: subscribing again does not double-register.
    assert register_archive_hook() is True
    assert registered_count() == 1


def test_registered_hook_receives_archive_fanout(monkeypatch, archived_file) -> None:
    from app.archive.hooks import on_archived as fanout

    calls: list = []
    monkeypatch.setattr(
        hooks,
        "process_pdf",
        lambda pdf, *, file_ref, display_name, settings=None: (
            calls.append(file_ref)
            or ExpirationOutcome(
                file_ref=file_ref, date="2027-01-01", upserted=True, status="written"
            )
        ),
    )
    register_archive_hook()
    # The archive agent's watcher fans out via on_archived(ArchivedFile) — our subscriber runs.
    delivered = fanout(archived_file)
    assert delivered == 1
    assert calls == ["drive-dest-9"]
