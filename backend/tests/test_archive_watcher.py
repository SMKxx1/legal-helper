"""The cache-folder watcher (over the ArchiveStorage provider) + classify + hooks (PLAN §3.10, ref §3.11).

Everything runs with zero network and zero LLM:

* the **watcher matrix** drives :func:`app.archive.watcher.run_watch_once` against an in-memory
  ``_FakeDrive`` (an :class:`~app.integrations.storage.base.ArchiveStorage` provider) + a stub classifier
  — the skip filters, the fail-closed claim/dedup, the rename golden, the classify-failure →
  ``saved_default_name`` fallback, the destination duplicate skip, the download ``failed`` status (file
  left in cache), the requester-DM seam, and the ``on_archived`` fan-out;
* the **classifier** rides a fake gateway adapter (harden matrix);
* the **hooks registry** fan-out is fail-soft per subscriber.

The concrete Google Drive backend's network matrix (token mint + list + upload + download + the error
taxonomy) lives in ``test_storage_drive.py`` — the watcher only sees the provider protocol.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from app.archive import hooks
from app.archive.classify import CacheClassification
from app.archive.models import NdaCacheProcessed
from app.archive.watcher import run_watch_once, should_skip
from app.config import Settings
from app.integrations.models import NdaEnvelope
from app.integrations.storage.base import (
    FOLDER_MIME_TYPE,
    PDF_MIME_TYPE,
    StorageEntry,
    StorageTerminalError,
    StoredFile,
)

pytest_plugins = ("conftest_bot",)

PDF = b"%PDF-1.4 signed\n%%EOF"
NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
TODAY = "20260704"


# --------------------------------------------------------------------------- #
# In-memory ArchiveStorage provider (implements the six-method protocol)
# --------------------------------------------------------------------------- #
def _entry(fid: str, name: str, mime: str = PDF_MIME_TYPE) -> StorageEntry:
    return StorageEntry(id=fid, name=name, mime_type=mime)


class _FakeDrive:
    """An in-memory :class:`ArchiveStorage` provider for the watcher matrix.

    ``subfolders``/``pdfs`` are keyed by parent folder id; ``list_folder`` returns whichever set
    ``mime_type`` selects (folders vs PDFs). ``uploads`` records ``(folder_id, name, content)`` for the
    filing assertions.
    """

    def __init__(self) -> None:
        self.folders_by_name: dict[str, str] = {}
        self.subfolders: dict[str, list[StorageEntry]] = {}
        self.pdfs: dict[str, list[StorageEntry]] = {}
        self.dest_existing: dict[
            str, dict[str, StorageEntry]
        ] = {}  # folder_id -> {name: entry}
        self.blobs: dict[str, bytes] = {}
        self.download_errors: set[str] = set()
        self.uploads: list[tuple[str, str, bytes]] = []
        self._next = 100

    def find_folder_by_name(self, name, *, parent_id=None):
        return self.folders_by_name.get(name)

    def list_folder(self, folder, *, by_name=False, mime_type=None):
        if mime_type == FOLDER_MIME_TYPE:
            return list(self.subfolders.get(folder, []))
        return list(self.pdfs.get(folder, []))

    def exists_in_folder(self, folder_id, name):
        return name in self.dest_existing.get(folder_id, {})

    def download(self, file_id):
        if file_id in self.download_errors:
            raise StorageTerminalError("download boom", status_code=404)
        return self.blobs.get(file_id, PDF)

    def upload(self, *, name, content, content_type, folder_id):
        self.uploads.append((folder_id, name, content))
        self._next += 1
        return StoredFile(id=f"UP{self._next}", name=name)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        drive_archive_folder_id="DEST",
        drive_cache_folder_name="Cache",
        nda_admin_slack_channel="C_ADMIN",
        nda_bot_from_email="nda-bot@example.com",
    )


def _drive_one_file(
    name: str = "signed.pdf", *, folder: str = "Envelope_E1"
) -> _FakeDrive:
    """A cache folder ``Cache`` (id CACHE) with one subfolder holding one PDF ``F1``."""
    d = _FakeDrive()
    d.folders_by_name["Cache"] = "CACHE"
    sub_id = "SUB1"
    d.subfolders["CACHE"] = [_entry(sub_id, folder, FOLDER_MIME_TYPE)]
    d.pdfs["CACHE"] = []
    d.pdfs[sub_id] = [_entry("F1", name)]
    d.blobs["F1"] = PDF
    return d


def _complete_classifier(**over):
    base = dict(
        issuer="Amperesand Inc",
        recipient="Acme Corp",
        nda_type="mNDA",
        effective_date="20260704",
    )
    base.update(over)
    return lambda text: CacheClassification(**base)


def _run(drive, *, session_factory, classify=None, **kw):
    return run_watch_once(
        settings=_settings(),
        session_factory=session_factory,
        drive=drive,
        classify=classify if classify is not None else _complete_classifier(),
        extract_text=lambda fn, data: "signed nda text long enough",
        notify_admin=kw.pop("notify_admin", lambda t: None),
        dm_requester=kw.pop("dm_requester", lambda ctx, t: None),
        now=NOW,
        **kw,
    )


@pytest.fixture(autouse=True)
def _clear_hooks():
    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


def _row(session_factory, file_id) -> NdaCacheProcessed:
    with session_factory() as db:
        return db.get(NdaCacheProcessed, file_id)


# --------------------------------------------------------------------------- #
# Skip filters
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name, skip",
    [
        ("Certificate of Completion.pdf", True),
        ("nda - certificate of completion.pdf", True),
        ("summary.pdf", True),
        ("Summary.PDF", True),
        ("signed nda.pdf", False),
        ("20260704_A_mNDA_B.pdf", False),
    ],
)
def test_should_skip(name, skip):
    assert should_skip(name) is skip


def test_watcher_skips_filtered_files(bot_session_factory):
    d = _drive_one_file()
    d.pdfs["SUB1"] = [
        _entry("C1", "Certificate of Completion.pdf"),
        _entry("S1", "summary.pdf"),
        _entry("F1", "signed.pdf"),
    ]
    stats = _run(d, session_factory=bot_session_factory)
    assert stats.scanned == 3
    assert stats.skipped == 2
    assert stats.claimed == 1
    assert _row(bot_session_factory, "C1") is None  # never claimed
    assert _row(bot_session_factory, "F1").status == "renamed"


# --------------------------------------------------------------------------- #
# Rename golden + fan-out
# --------------------------------------------------------------------------- #
def test_watcher_renames_and_files(bot_session_factory):
    d = _drive_one_file()
    got: list[Any] = []
    hooks.register_on_archived(got.append)

    stats = _run(d, session_factory=bot_session_factory)

    golden = "20260704_Amperesand Inc_mNDA_Acme Corp.pdf"
    assert stats.renamed == 1
    assert d.uploads == [("DEST", golden, PDF)]
    row = _row(bot_session_factory, "F1")
    assert row.status == "renamed"
    assert row.renamed_to == golden
    assert row.envelope_folder == "Envelope_E1"
    # Fan-out fired with the filed name + the downloaded bytes.
    assert len(got) == 1
    assert got[0].file_name == golden
    assert got[0].pdf_bytes == PDF
    assert got[0].source_file_id == "F1"
    assert got[0].classification["nda_type"] == "mNDA"


# --------------------------------------------------------------------------- #
# Classify failure → saved_default_name
# --------------------------------------------------------------------------- #
def test_watcher_saves_default_name_when_classify_incomplete(bot_session_factory):
    d = _drive_one_file(name="signed.pdf")
    incomplete = _complete_classifier(issuer="")  # missing issuer → not complete
    stats = _run(d, session_factory=bot_session_factory, classify=incomplete)
    assert stats.saved_default_name == 1
    assert d.uploads == [("DEST", "signed.pdf", PDF)]  # original name
    assert _row(bot_session_factory, "F1").status == "saved_default_name"


def test_watcher_saves_default_name_when_classify_raises(bot_session_factory):
    d = _drive_one_file(name="signed.pdf")

    def _boom(text):
        raise RuntimeError("provider down")

    stats = _run(d, session_factory=bot_session_factory, classify=_boom)
    assert stats.saved_default_name == 1
    assert _row(bot_session_factory, "F1").status == "saved_default_name"


def test_watcher_saves_default_name_when_no_llm(bot_session_factory):
    # classify=None (no LLM configured) → file under original name.
    d = _drive_one_file(name="signed.pdf")
    stats = run_watch_once(
        settings=_settings(),
        session_factory=bot_session_factory,
        drive=d,
        classify=None,
        extract_text=lambda fn, data: "text",
        notify_admin=lambda t: None,
        dm_requester=lambda c, t: None,
        now=NOW,
    )
    assert stats.saved_default_name == 1
    assert _row(bot_session_factory, "F1").status == "saved_default_name"


# --------------------------------------------------------------------------- #
# Duplicate skip
# --------------------------------------------------------------------------- #
def test_watcher_skips_destination_duplicate(bot_session_factory):
    d = _drive_one_file()
    golden = "20260704_Amperesand Inc_mNDA_Acme Corp.pdf"
    d.dest_existing["DEST"] = {golden: _entry("EXIST", golden)}
    notices: list[str] = []
    stats = _run(d, session_factory=bot_session_factory, notify_admin=notices.append)
    assert stats.duplicate_skipped == 1
    assert d.uploads == []  # never re-uploaded
    assert _row(bot_session_factory, "F1").status == "duplicate_skipped"
    assert any("Skipped" in n for n in notices)


# --------------------------------------------------------------------------- #
# Download failure → failed (left in cache)
# --------------------------------------------------------------------------- #
def test_watcher_marks_failed_on_download_error(bot_session_factory):
    d = _drive_one_file()
    d.download_errors.add("F1")
    notices: list[str] = []
    stats = _run(d, session_factory=bot_session_factory, notify_admin=notices.append)
    assert stats.failed == 1
    assert d.uploads == []
    row = _row(bot_session_factory, "F1")
    assert row.status == "failed"
    assert row.error and "download" in row.error
    assert any("Couldn't file" in n for n in notices)


# --------------------------------------------------------------------------- #
# Fail-closed dedup across passes
# --------------------------------------------------------------------------- #
def test_watcher_dedups_across_passes(bot_session_factory):
    d = _drive_one_file()
    first = _run(d, session_factory=bot_session_factory)
    assert first.claimed == 1
    # Same file id present again on the next poll → claimed 0 (already in nda_cache_processed).
    d.uploads.clear()
    second = _run(d, session_factory=bot_session_factory)
    assert second.claimed == 0
    assert d.uploads == []


# --------------------------------------------------------------------------- #
# Capability gate
# --------------------------------------------------------------------------- #
def test_watcher_disabled_without_drive_config(bot_session_factory):
    # No drive injected + no Drive config → the capability gate makes it a clean no-op.
    stats = run_watch_once(
        settings=Settings(_env_file=None),
        session_factory=bot_session_factory,
        now=NOW,
    )
    assert stats.disabled is True
    assert stats.claimed == 0


# --------------------------------------------------------------------------- #
# Requester-DM seam (STUBBED — envelope-id from folder → nda_envelopes)
# --------------------------------------------------------------------------- #
def test_envelope_id_from_folder_matches_ported_regex():
    # Ported /^Envelope[_ ]?/i: bare GUID (the common DocuSign archive case) passes through; an
    # optional case-insensitive Envelope/Envelope_/"Envelope " prefix is stripped; root '' -> ''.
    from app.archive.naming import envelope_id_from_folder

    assert (
        envelope_id_from_folder("abc-123-def") == "abc-123-def"
    )  # bare GUID unchanged
    assert envelope_id_from_folder("Envelope_abc-123") == "abc-123"
    assert envelope_id_from_folder("Envelope abc-123") == "abc-123"  # space separator
    assert envelope_id_from_folder("envelope_abc-123") == "abc-123"  # case-insensitive
    assert envelope_id_from_folder("EnvelopeABC") == "ABC"  # no separator
    assert envelope_id_from_folder("") == ""  # cache root
    assert envelope_id_from_folder("  Envelope_x  ") == "x"  # trimmed


def test_watcher_dms_requester_when_envelope_matches(bot_session_factory):
    with bot_session_factory() as db:
        db.add(
            NdaEnvelope(
                envelope_id="E1",
                idempotency_key="k1",
                status="sent",
                channel="slack",
                routing="all_at_once",
                requested_by="U9",
                signer_emails=[],
                cc_emails=[],
                slack_channel="C9",
                slack_thread_ts="T9",
            )
        )
        db.commit()
    d = _drive_one_file(folder="Envelope_E1")
    dms: list[tuple[dict, str]] = []
    _run(
        d,
        session_factory=bot_session_factory,
        dm_requester=lambda ctx, t: dms.append((ctx, t)),
    )
    assert len(dms) == 1
    assert dms[0][0]["target"] == "U9"
    assert dms[0][0]["kind"] == "slack"


def test_watcher_no_dm_for_cache_root_file(bot_session_factory):
    # A file directly in the cache root (folder name '') has no envelope id → no DM.
    d = _FakeDrive()
    d.folders_by_name["Cache"] = "CACHE"
    d.subfolders["CACHE"] = []
    d.pdfs["CACHE"] = [_entry("F1", "signed.pdf")]
    d.blobs["F1"] = PDF
    dms: list[Any] = []
    _run(
        d,
        session_factory=bot_session_factory,
        dm_requester=lambda ctx, t: dms.append(ctx),
    )
    assert dms == []


# --------------------------------------------------------------------------- #
# hooks registry
# --------------------------------------------------------------------------- #
def test_hooks_fan_out_is_failsoft():
    hooks.clear_hooks()
    seen: list[str] = []

    def _bad(_f):
        raise RuntimeError("subscriber boom")

    def _good(f):
        seen.append(f.file_name)

    hooks.register_on_archived(_bad)
    hooks.register_on_archived(_good)
    try:
        delivered = hooks.on_archived(hooks.ArchivedFile(file_name="x.pdf"))
        assert delivered == 1  # only the good one counted; the bad one was swallowed
        assert seen == ["x.pdf"]
    finally:
        hooks.clear_hooks()


def test_hooks_register_is_idempotent():
    hooks.clear_hooks()

    def _h(_f):
        pass

    hooks.register_on_archived(_h)
    hooks.register_on_archived(_h)
    assert hooks.registered_count() == 1
    hooks.unregister_on_archived(_h)
    assert hooks.registered_count() == 0


# --------------------------------------------------------------------------- #
# classify — fake gateway adapter
# --------------------------------------------------------------------------- #
class _FakeAdapter:
    name = "fake"
    model_id = "fake/classify"

    def __init__(self, obj) -> None:
        self._text = obj if isinstance(obj, str) else json.dumps(obj)

    def complete(self, req):
        from app.ai.gateway import RawResult, Usage

        return RawResult(text=self._text, usage=Usage(), model_version=self.model_id)


def test_classify_nda_hardens_output():
    from app.ai.gateway import Gateway
    from app.archive.classify import classify_nda

    gw = Gateway(
        _FakeAdapter(
            {
                "reasoning": "amperesand is the discloser; two-way",
                "issuer": "Amperesand, Inc.",
                "recipient": "Acme Corp",
                "nda_type": "mutual",  # synonym → mNDA
                "counterparty_name": "Acme Corp",
                "effective_date": "20260704",
            }
        )
    )
    c = classify_nda("some signed nda text", gateway=gw)
    assert c.issuer == "Amperesand Inc"  # comma/period stripped
    assert c.nda_type == "mNDA"
    assert c.is_complete is True


def test_classify_nda_incomplete_when_bad_type():
    from app.ai.gateway import Gateway
    from app.archive.classify import classify_nda

    gw = Gateway(
        _FakeAdapter(
            {
                "reasoning": "unclear",
                "issuer": "A Co",
                "recipient": "B Co",
                "nda_type": "unknown-type",
                "counterparty_name": "B Co",
                "effective_date": "not-a-date",
            }
        )
    )
    c = classify_nda("t", gateway=gw)
    assert c.nda_type == ""
    assert c.effective_date == ""
    assert c.is_complete is False
