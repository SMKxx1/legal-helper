"""Expiration orchestration core (extract → upsert) — the outcome matrix (PLAN §3.10).

NO network: the extractor + Airtable calls ride injected ``httpx.MockTransport``s. Pins that each
capability fails soft INDEPENDENTLY: LLM off, extraction-without-a-date, Airtable off, and Airtable
write error each produce a distinct typed outcome and never raise.
"""

from __future__ import annotations

import httpx

from app.config import Settings
from app.expiration.service import process_pdf

PDF = b"%PDF-1.4 fake"


def mk_settings(*, llm: bool = True, airtable: bool = True) -> Settings:
    kw: dict = {}
    if llm:
        kw["openrouter_api_key"] = "sk-or-test"
    if airtable:
        kw.update(airtable_pat="pat", airtable_base_id="appX", airtable_table="Exp")
    return Settings(_env_file=None, **kw)


def _one(response: dict | httpx.Response):
    def handler(_req: httpx.Request) -> httpx.Response:
        return (
            response
            if isinstance(response, httpx.Response)
            else httpx.Response(200, json=response)
        )

    return httpx.MockTransport(handler)


def _or(content: str) -> httpx.MockTransport:
    return _one(
        {
            "model": "google/gemini-3.5-flash",
            "choices": [{"message": {"content": content}}],
        }
    )


def test_written_when_date_and_airtable_ok() -> None:
    out = process_pdf(
        PDF,
        file_ref="file1",
        display_name="ACME.pdf",
        settings=mk_settings(),
        extract_transport=_or("2027-05-01"),
        airtable_transport=_one({"records": [{"id": "recA"}]}),
    )
    assert out.status == "written"
    assert out.date == "2027-05-01"
    assert out.upserted is True


def test_no_date_when_model_returns_error() -> None:
    out = process_pdf(
        PDF,
        file_ref="file1",
        display_name="ACME.pdf",
        settings=mk_settings(),
        extract_transport=_or("ERROR"),
        airtable_transport=_one({"records": [{"id": "should-not-be-called"}]}),
    )
    assert out.status == "no_date"
    assert out.date is None
    assert out.upserted is False


def test_llm_off_is_clean_noop() -> None:
    out = process_pdf(
        PDF, file_ref="file1", display_name="ACME.pdf", settings=mk_settings(llm=False)
    )
    assert out.status == "llm_off"
    assert out.upserted is False


def test_airtable_off_keeps_the_extracted_date() -> None:
    out = process_pdf(
        PDF,
        file_ref="file1",
        display_name="ACME.pdf",
        settings=mk_settings(airtable=False),
        extract_transport=_or("2027-05-01"),
    )
    assert out.status == "airtable_off"
    assert out.date == "2027-05-01"  # extracted, just not persisted
    assert out.upserted is False


def test_airtable_write_error_surfaces_the_date() -> None:
    out = process_pdf(
        PDF,
        file_ref="file1",
        display_name="ACME.pdf",
        settings=mk_settings(),
        extract_transport=_or("2027-05-01"),
        airtable_transport=_one(httpx.Response(503, text="airtable down")),
    )
    assert out.status == "airtable_error"
    assert out.date == "2027-05-01"
    assert out.upserted is False
