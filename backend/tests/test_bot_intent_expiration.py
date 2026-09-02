"""Manual expiration Slack commands — parse matrix + handler behavior (PLAN §3.10 trigger c).

Pins: the command grammar (``set …`` / ``re-extract …`` phrasings), the no-LLM manual override write,
the Airtable-off degrade, and the re-extract path through the archive seam. All with injected stubs and
zero network.
"""

from __future__ import annotations

import pytest

from app.bot.envelope import Envelope
from app.bot.intents import IntentContext
from app.bot.intents.expiration import (
    TRACKER_OFF_TEXT,
    USAGE_TEXT,
    ExpirationIntent,
    matches_expiration_command,
    parse_expiration_command,
)
from app.bot.router import Classification
from app.config import Settings
from app.expiration.jobs import ArchivedFile
from app.expiration.service import ExpirationOutcome
from app.integrations.airtable import (
    FIELD_EXPIRATION_DATE,
    AirtableRetryableError,
    AirtableUnavailable,
)


def ctx(text: str) -> IntentContext:
    return IntentContext(
        envelope=Envelope(channel="slack", event_key="slack:1", text=text),
        classification=Classification(intent="expiration"),
    )


def settings() -> Settings:
    return Settings(_env_file=None)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,file,date",
    [
        ("set expiration of ACME_NDA.pdf to 2027-01-15", "ACME_NDA.pdf", "2027-01-15"),
        (
            "set the expiration date of My NDA.pdf to 2030-12-31",
            "My NDA.pdf",
            "2030-12-31",
        ),
        ("set ACME_NDA.pdf expiration to 2027-01-15", "ACME_NDA.pdf", "2027-01-15"),
        (
            "please set expiration for foo.pdf to 2025-06-01 thanks",
            "foo.pdf",
            "2025-06-01",
        ),
        ("expiration of foo.pdf is 2026-03-03", "foo.pdf", "2026-03-03"),
    ],
)
def test_parse_set(text: str, file: str, date: str) -> None:
    cmd = parse_expiration_command(text)
    assert cmd is not None
    assert cmd.action == "set"
    assert cmd.file == file
    assert cmd.date == date


@pytest.mark.parametrize(
    "text,file",
    [
        ("re-extract expiration of SG_Company_NA__01.pdf", "SG_Company_NA__01.pdf"),
        ("reextract the expiration date for foo.pdf", "foo.pdf"),
        ("re-run expiration on bar.pdf", "bar.pdf"),
    ],
)
def test_parse_reextract(text: str, file: str) -> None:
    cmd = parse_expiration_command(text)
    assert cmd is not None
    assert cmd.action == "reextract"
    assert cmd.file == file
    assert cmd.date == ""


@pytest.mark.parametrize(
    "text",
    [
        "hello there",
        "archive this NDA",
        "set expiration of foo.pdf to next tuesday",  # non-ISO date -> not a valid set command
        "",
    ],
)
def test_parse_negatives(text: str) -> None:
    assert parse_expiration_command(text) is None
    assert matches_expiration_command(text) is False


def test_quoted_and_bracketed_filenames_cleaned() -> None:
    assert (
        parse_expiration_command('set expiration of "ACME.pdf" to 2027-01-01').file
        == "ACME.pdf"
    )
    assert (
        parse_expiration_command("set expiration of <ACME.pdf> to 2027-01-01").file
        == "ACME.pdf"
    )


# --------------------------------------------------------------------------- #
# SET handler (manual override, no LLM)
# --------------------------------------------------------------------------- #
def test_set_writes_to_airtable_no_llm() -> None:
    calls: list = []

    def fake_upsert(file_ref, fields, *, settings=None, registry=None, transport=None):
        calls.append((file_ref, fields))
        return {"id": "rec1"}

    intent = ExpirationIntent(settings=settings(), upsert=fake_upsert)
    reply = intent(ctx("set expiration of ACME_NDA.pdf to 2027-01-15"))
    assert "ACME_NDA.pdf" in reply.text
    assert "2027-01-15" in reply.text
    assert len(calls) == 1
    file_ref, fields = calls[0]
    assert file_ref == "ACME_NDA.pdf"
    assert fields[FIELD_EXPIRATION_DATE] == "2027-01-15"


def test_set_tracker_off_is_friendly() -> None:
    def fake_upsert(*a, **k):
        raise AirtableUnavailable("off")

    intent = ExpirationIntent(settings=settings(), upsert=fake_upsert)
    reply = intent(ctx("set expiration of x.pdf to 2027-01-15"))
    assert reply.text == TRACKER_OFF_TEXT


def test_set_write_error_is_friendly() -> None:
    def fake_upsert(*a, **k):
        raise AirtableRetryableError("boom")

    intent = ExpirationIntent(settings=settings(), upsert=fake_upsert)
    reply = intent(ctx("set expiration of x.pdf to 2027-01-15"))
    assert "couldn't save" in reply.text.lower()


# --------------------------------------------------------------------------- #
# RE-EXTRACT handler
# --------------------------------------------------------------------------- #
class FakeSource:
    def __init__(self, files: list[ArchivedFile]) -> None:
        self._files = files
        self.downloaded: list[str] = []

    def list_pdfs(self):
        return list(self._files)

    def download(self, file_ref: str) -> bytes:
        self.downloaded.append(file_ref)
        return b"%PDF"


def test_reextract_found_and_written() -> None:
    source = FakeSource([ArchivedFile("driveid1", "MyNDA.pdf")])
    seen: dict = {}

    def fake_process(pdf_bytes, *, file_ref, display_name, settings=None, **k):
        seen.update(file_ref=file_ref, display_name=display_name)
        return ExpirationOutcome(
            file_ref=file_ref, date="2028-02-02", upserted=True, status="written"
        )

    intent = ExpirationIntent(
        settings=settings(), source_resolver=lambda s: source, process=fake_process
    )
    reply = intent(ctx("re-extract expiration of MyNDA.pdf"))
    assert "2028-02-02" in reply.text
    assert source.downloaded == ["driveid1"]
    assert seen == {"file_ref": "driveid1", "display_name": "MyNDA.pdf"}


def test_reextract_no_source_is_friendly() -> None:
    intent = ExpirationIntent(settings=settings(), source_resolver=lambda s: None)
    reply = intent(ctx("re-extract expiration of MyNDA.pdf"))
    assert "archive" in reply.text.lower()


def test_reextract_not_found_is_friendly() -> None:
    source = FakeSource([ArchivedFile("driveid1", "Other.pdf")])
    intent = ExpirationIntent(settings=settings(), source_resolver=lambda s: source)
    reply = intent(ctx("re-extract expiration of MyNDA.pdf"))
    assert "couldn't find" in reply.text.lower()


def test_reextract_no_date_is_friendly() -> None:
    source = FakeSource([ArchivedFile("driveid1", "MyNDA.pdf")])

    def fake_process(pdf_bytes, *, file_ref, display_name, settings=None, **k):
        return ExpirationOutcome(
            file_ref=file_ref, date=None, upserted=False, status="no_date"
        )

    intent = ExpirationIntent(
        settings=settings(), source_resolver=lambda s: source, process=fake_process
    )
    reply = intent(ctx("re-extract expiration of MyNDA.pdf"))
    assert "couldn't determine" in reply.text.lower()


# --------------------------------------------------------------------------- #
# Unrecognized
# --------------------------------------------------------------------------- #
def test_unrecognized_command_shows_usage() -> None:
    intent = ExpirationIntent(settings=settings())
    reply = intent(ctx("do something with expirations"))
    assert reply.text == USAGE_TEXT
