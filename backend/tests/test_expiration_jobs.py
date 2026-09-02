"""Nightly expiration sweep + scheduler registration (PLAN §3.10 trigger b).

NO network: a fake ``ArchiveSource`` supplies the archived PDFs; the LLM + Airtable calls ride injected
``httpx.MockTransport``s. Pins: already-tracked NDAs are skipped (one Airtable list, zero LLM calls),
untracked ones are extracted + written, capability gates no-op cleanly, and
``register_expiration_jobs`` schedules the cron at ``expiration_sweep_hour_utc``.
"""

from __future__ import annotations

import httpx

from app.config import Settings
from app.expiration.jobs import (
    ArchivedFile,
    SweepReport,
    register_expiration_jobs,
    run_expiration_sweep,
)


def mk_settings(
    *, llm: bool = True, airtable: bool = True, drive: bool = False, sweep_hour: int = 2
) -> Settings:
    kw: dict = {"expiration_sweep_hour_utc": sweep_hour}
    if llm:
        kw["openrouter_api_key"] = "sk-or-test"
    if airtable:
        kw.update(airtable_pat="pat", airtable_base_id="appX", airtable_table="Exp")
    if drive:
        kw.update(
            google_oauth_client_id="cid",
            google_oauth_client_secret="csecret",
            google_oauth_refresh_token="rtok",
            drive_archive_folder_id="archive-folder-1",
        )
    return Settings(_env_file=None, **kw)


class FakeSource:
    """A stand-in for the archive agent's Drive-backed ArchiveSource."""

    def __init__(
        self, files: list[ArchivedFile], blobs: dict[str, bytes] | None = None
    ) -> None:
        self._files = files
        self._blobs = blobs or {
            f.file_ref: b"%PDF-1.4 " + f.file_ref.encode() for f in files
        }
        self.downloaded: list[str] = []

    def list_pdfs(self):
        return list(self._files)

    def download(self, file_ref: str) -> bytes:
        self.downloaded.append(file_ref)
        return self._blobs[file_ref]


def airtable_transport(tracked: list[dict], *, upsert_status: int = 200):
    """MockTransport for the sweep's Airtable client: GET list_tracked, PATCH upserts."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"records": tracked})
        if upsert_status != 200:
            return httpx.Response(upsert_status, text="airtable down")
        return httpx.Response(200, json={"records": [{"id": "recNew"}]})

    return httpx.MockTransport(handler)


def or_transport(date: str = "2027-09-09"):
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"model": "g", "choices": [{"message": {"content": date}}]}
        )

    return httpx.MockTransport(handler)


def _rec(file_ref: str, date: str = "") -> dict:
    return {
        "id": "r" + file_ref,
        "fields": {"File Id": file_ref, "Expiration Date": date},
    }


# --------------------------------------------------------------------------- #
# Core sweep logic
# --------------------------------------------------------------------------- #
def test_skips_tracked_extracts_untracked() -> None:
    source = FakeSource([ArchivedFile("f1", "A.pdf"), ArchivedFile("f2", "B.pdf")])
    report = run_expiration_sweep(
        mk_settings(),
        source=source,
        extract_transport=or_transport("2027-09-09"),
        airtable_transport=airtable_transport(
            [_rec("f1", "2027-01-01")]
        ),  # f1 already dated
    )
    assert report.scanned == 2
    assert report.skipped_tracked == 1
    assert report.written == 1
    assert report.no_date == 0
    assert report.failed == 0
    # Only the untracked file was downloaded + extracted.
    assert source.downloaded == ["f2"]


def test_tracked_but_empty_date_is_reprocessed() -> None:
    source = FakeSource([ArchivedFile("f1", "A.pdf")])
    report = run_expiration_sweep(
        mk_settings(),
        source=source,
        extract_transport=or_transport("2028-08-08"),
        airtable_transport=airtable_transport(
            [_rec("f1", "")]
        ),  # tracked, no date -> reprocess
    )
    assert report.skipped_tracked == 0
    assert report.written == 1
    assert source.downloaded == ["f1"]


def test_error_output_counts_as_no_date() -> None:
    source = FakeSource([ArchivedFile("f1", "A.pdf")])
    report = run_expiration_sweep(
        mk_settings(),
        source=source,
        extract_transport=or_transport("ERROR"),
        airtable_transport=airtable_transport([]),
    )
    assert report.written == 0
    assert report.no_date == 1
    assert report.processed == 1


def test_download_failure_is_counted_not_fatal() -> None:
    class Broken(FakeSource):
        def download(self, file_ref: str) -> bytes:
            raise OSError("drive read failed")

    source = Broken([ArchivedFile("f1", "A.pdf"), ArchivedFile("f2", "B.pdf")])
    report = run_expiration_sweep(
        mk_settings(),
        source=source,
        extract_transport=or_transport(),
        airtable_transport=airtable_transport([]),
    )
    assert report.failed == 2
    assert report.written == 0


# --------------------------------------------------------------------------- #
# Capability gates (fail soft)
# --------------------------------------------------------------------------- #
def test_llm_off_noops() -> None:
    report = run_expiration_sweep(
        mk_settings(llm=False), source=FakeSource([ArchivedFile("f1", "A")])
    )
    assert report == SweepReport(status="llm_off")


def test_airtable_off_noops() -> None:
    report = run_expiration_sweep(
        mk_settings(airtable=False), source=FakeSource([ArchivedFile("f1", "A")])
    )
    assert report.status == "airtable_off"


def test_no_archive_source_noops_when_drive_unconfigured() -> None:
    # source=None + no Google Drive config -> _resolve_archive_source returns None (no network) -> drive_off.
    report = run_expiration_sweep(
        mk_settings(drive=False), airtable_transport=airtable_transport([])
    )
    assert report.status == "drive_off"


# --------------------------------------------------------------------------- #
# Real storage-backed source (adapts the GoogleDriveStorage provider) end-to-end
# --------------------------------------------------------------------------- #
def drive_transport(files: list[dict], blobs: dict[str, bytes]):
    """MockTransport for the GoogleDriveStorage provider: token mint, list_folder, download."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "oauth2.googleapis.com/token" in url:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if request.method == "GET" and "alt=media" in url:
            file_id = url.split("/files/")[1].split("?")[0]
            return httpx.Response(200, content=blobs[file_id])
        if request.method == "GET":  # list files
            return httpx.Response(200, json={"files": files})
        return httpx.Response(400, json={"error": {"message": "unexpected"}})

    return httpx.MockTransport(handler)


def test_sweep_over_real_storage_provider_source() -> None:
    # No injected source -> _resolve_archive_source builds the GoogleDriveStorage adapter over the folder.
    report = run_expiration_sweep(
        mk_settings(drive=True),
        extract_transport=or_transport("2029-03-03"),
        airtable_transport=airtable_transport([]),  # nothing tracked yet
        drive_transport=drive_transport(
            files=[{"id": "d1", "name": "A.pdf"}, {"id": "d2", "name": "B.pdf"}],
            blobs={"d1": b"%PDF-a", "d2": b"%PDF-b"},
        ),
    )
    assert report.scanned == 2
    assert report.written == 2
    assert report.skipped_tracked == 0


# --------------------------------------------------------------------------- #
# Scheduler registration
# --------------------------------------------------------------------------- #
class FakeSched:
    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def add_job(self, func, trigger, **kw) -> None:
        self.jobs.append({"func": func, "trigger": trigger, **kw})


def test_register_expiration_jobs_adds_nightly_cron() -> None:
    sched = FakeSched()
    register_expiration_jobs(sched, mk_settings(sweep_hour=3))
    assert len(sched.jobs) == 1
    job = sched.jobs[0]
    assert job["trigger"] == "cron"
    assert job["hour"] == 3
    assert job["minute"] == 0
    assert job["id"] == "expiration_sweep"
    assert job["func"] is run_expiration_sweep
    assert job["max_instances"] == 1
