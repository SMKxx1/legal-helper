"""Bulk template upload + the downloadable token-reference PDF (admin Templates page).

Mounts the REAL app (``client`` fixture) on a throwaway DB; ``login`` logs an admin in + sets the CSRF
header so the state-changing POST passes. Mirrors ``test_studio_pages`` conventions.
"""

from __future__ import annotations

import pytest
from conftest_admin import seed_catalog, source_docx

from app.api.routes_studio import parse_template_filename
from app.models_v2 import TemplateVersion

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _admin(client, seed_user, login):
    seed_user("admin", role="admin")
    assert login("admin").status_code == 200


def _docx(name: str):
    return (name, source_docx(), _DOCX_MIME)


# --------------------------------------------------------------------------- #
# Filename parser
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name,expected",
    [
        ("SG_Company.docx", ("SG", "Company", "", "empty")),
        ("US_Company.docx", ("US", "Company", "", "empty")),
        ("US_Individual_Mutual.docx", ("US", "Individual", "Mutual", "empty")),
        ("SG_Individual_Unilateral.docx", ("SG", "Individual", "Unilateral", "empty")),
        ("SG_ServiceProvider.docx", ("SG", "ServiceProvider", "", "empty")),
        ("SG_SP.docx", ("SG", "ServiceProvider", "", "empty")),  # SP alias -> canonical
        ("US_Company_tokenised.docx", ("US", "Company", "", "tokenised")),
        # case-insensitive input, canonical output:
        ("us_company_TOKENIZED.docx", ("US", "Company", "", "tokenised")),
        ("SG_Individual_Mutual_empty.docx", ("SG", "Individual", "Mutual", "empty")),
    ],
)
def test_parse_template_filename(name, expected) -> None:
    assert parse_template_filename(name) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        ".docx",
        "SG.docx",
        "onlyone.docx",
        "_.docx",
        "XX_Company.docx",  # unknown jurisdiction
        "SG_Partnership.docx",  # unknown counterparty type
        "SG_Company_Mutual.docx",  # mutuality on a non-Individual counterparty
        "US_Individual.docx",  # Individual missing its (required) mutuality
        "US_Individual_Unilaterl.docx",  # mutuality typo — must NOT silently resolve to Mutual
        "SG_ServiceProvider_tokenisd.docx",  # variant typo — must NOT silently fall back to empty
        "SG_Company_Individual_Mutual.docx",  # too many segments
    ],
)
def test_parse_template_filename_rejects_bad(bad) -> None:
    with pytest.raises(ValueError):
        parse_template_filename(bad)


# --------------------------------------------------------------------------- #
# Bulk upload endpoint
# --------------------------------------------------------------------------- #
def test_bulk_upload_mixed_results(client, db, seed_user, login) -> None:
    seed_catalog(db)
    _admin(client, seed_user, login)
    resp = client.post(
        "/admin/templates/bulk-upload",
        files=[
            ("files", _docx("SG_Company.docx")),
            ("files", _docx("US_Individual_Mutual.docx")),
            ("files", _docx("SG_ServiceProvider_tokenised.docx")),
            ("files", _docx("garbage.docx")),  # unparseable name
            ("files", ("notes.txt", b"not a docx", "text/plain")),  # not .docx
        ],
    )
    assert resp.status_code == 200
    results = {r["filename"]: r for r in resp.json()["results"]}

    assert results["SG_Company.docx"]["ok"] is True
    assert results["SG_Company.docx"]["combo"] == "SG / Company / NotApplicable"
    assert results["SG_Company.docx"]["variant"] == "empty"

    assert results["US_Individual_Mutual.docx"]["ok"] is True
    assert results["US_Individual_Mutual.docx"]["combo"] == "US / Individual / Mutual"

    # A filename variant suffix is now TOLERATED (still parses, no error) but IGNORED — the batch
    # variant selector controls it, defaulting to "empty" when no variant field is posted.
    assert results["SG_ServiceProvider_tokenised.docx"]["ok"] is True
    assert results["SG_ServiceProvider_tokenised.docx"]["variant"] == "empty"

    assert results["garbage.docx"]["ok"] is False and results["garbage.docx"]["error"]
    assert results["notes.txt"]["ok"] is False

    # A valid file actually created a NON-current draft version in the right slot.
    version = db.get(TemplateVersion, results["SG_Company.docx"]["version_id"])
    assert version is not None
    assert version.is_current is False
    assert version.variant_code == "empty"


def test_bulk_upload_variant_selector_is_authoritative(
    client, db, seed_user, login
) -> None:
    seed_catalog(db)
    _admin(client, seed_user, login)
    resp = client.post(
        "/admin/templates/bulk-upload",
        data={"variant": "tokenised"},
        files=[
            ("files", _docx("SG_Company.docx")),
            ("files", _docx("US_Individual_Mutual.docx")),
        ],
    )
    assert resp.status_code == 200
    results = {r["filename"]: r for r in resp.json()["results"]}
    assert results["SG_Company.docx"]["variant"] == "tokenised"
    assert results["US_Individual_Mutual.docx"]["variant"] == "tokenised"


def test_bulk_upload_rejects_unknown_variant(client, db, seed_user, login) -> None:
    seed_catalog(db)
    _admin(client, seed_user, login)
    resp = client.post(
        "/admin/templates/bulk-upload",
        data={"variant": "bogus"},
        files=[("files", _docx("SG_Company.docx"))],
    )
    assert resp.status_code == 400


def test_bulk_upload_unseeded_combo_reported_per_file(
    client, db, seed_user, login
) -> None:
    # No seed_catalog: the combo is valid but has no template row -> a friendly per-file error.
    _admin(client, seed_user, login)
    resp = client.post(
        "/admin/templates/bulk-upload",
        files=[("files", _docx("SG_Company.docx"))],
    )
    assert resp.status_code == 200
    row = resp.json()["results"][0]
    assert row["ok"] is False
    assert "template" in row["error"].lower()


def test_bulk_upload_requires_admin(client, db) -> None:
    seed_catalog(db)
    resp = client.post(
        "/admin/templates/bulk-upload",
        files=[("files", _docx("SG_Company.docx"))],
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Token-reference PDF
# --------------------------------------------------------------------------- #
def test_token_reference_pdf_downloads(client, seed_user, login) -> None:
    _admin(client, seed_user, login)
    resp = client.get("/admin/templates/token-reference.pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.content[:4] == b"%PDF"
    assert len(resp.content) > 1000


def test_token_reference_pdf_anonymous_redirects_to_login(client) -> None:
    resp = client.get("/admin/templates/token-reference.pdf", follow_redirects=False)
    assert resp.status_code == 303  # admin_page redirects an anonymous user to login
