"""Token-registry CRUD + validation + delete usage-report matrix (PLAN §3.7).

Covers :mod:`app.registry.tokens`: create (snake_case validation, uniqueness across name+placeholder),
metadata update, and the delete safety gate — the usage report (template-blob scan + form bindings) plus
the ``force`` requirement. Zero network; the throwaway per-test SQLite DB is provided by ``conftest``.
"""

from __future__ import annotations

import hashlib
import uuid
from io import BytesIO

import pytest

from app.models_v2 import DocumentBlob, Template, TemplateVersion, Token
from app.registry import tokens as reg
from app.registry.models import TokenMeta
from app.schemas import DEFAULT_ORG_ID
from app.support_task.generator import DOCX_MIME


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_docx(*paragraphs: str) -> bytes:
    from docx import Document

    doc = Document()
    for para in paragraphs:
        doc.add_paragraph(para)
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _seed_template_blob(
    db, docx_bytes: bytes, *, variant: str = "tokenised"
) -> TemplateVersion:
    blob = DocumentBlob(
        sha256=hashlib.sha256(docx_bytes + uuid.uuid4().bytes).hexdigest(),
        byte_size=len(docx_bytes),
        mime_type=DOCX_MIME,
        bytes=docx_bytes,
    )
    db.add(blob)
    db.flush()
    tmpl = Template(
        org_id=DEFAULT_ORG_ID,
        jurisdiction_code="US",
        counterparty_type_code="Company",
        mutuality_code="NotApplicable",
        name="US Company NDA",
    )
    db.add(tmpl)
    db.flush()
    tv = TemplateVersion(
        template_id=tmpl.id,
        variant_code=variant,
        version_no=1,
        blob_id=blob.id,
        is_current=True,
    )
    db.add(tv)
    db.commit()
    return tv


# --------------------------------------------------------------------------- #
# Create + validation
# --------------------------------------------------------------------------- #
def test_create_token_persists_token_and_meta(db) -> None:
    view = reg.create_token(
        db,
        name="counterparty_alias",
        label="Counterparty alias",
        help_text="A short name for the counterparty.",
        data_type="text",
        party="counterparty",
        fallback_text="the Counterparty",
        created_by="admin@example.com",
    )
    assert view.name == "counterparty_alias"
    assert view.placeholder == "{{counterparty_alias}}"
    assert view.party == "counterparty"
    assert view.fallback_text == "the Counterparty"

    tok = db.execute(
        Token.__table__.select().where(Token.name == "counterparty_alias")
    ).first()
    assert tok is not None
    meta = db.get(TokenMeta, view.id)
    assert meta is not None
    assert meta.label == "Counterparty alias"
    # Ported Token.description is kept mirrored from help_text so generator hints still populate.
    assert (
        reg.get_token(db, "counterparty_alias").description
        == "A short name for the counterparty."
    )


@pytest.mark.parametrize(
    "bad_name",
    [
        "Counterparty",
        "counter party",
        "_leading",
        "trailing_",
        "double__underscore",
        "9leading",
        "{{brace}}",
        "",
    ],
)
def test_create_token_rejects_non_snake_case(db, bad_name) -> None:
    with pytest.raises(reg.TokenValidationError):
        reg.create_token(db, name=bad_name)


def test_create_token_rejects_bad_data_type_and_party(db) -> None:
    with pytest.raises(reg.TokenValidationError):
        reg.create_token(db, name="ok_name_a", data_type="datetime")
    with pytest.raises(reg.TokenValidationError):
        reg.create_token(db, name="ok_name_b", party="internal_secret")


def test_create_token_uniqueness(db) -> None:
    reg.create_token(db, name="dup_token")
    with pytest.raises(reg.TokenExistsError):
        reg.create_token(db, name="dup_token")


def test_update_meta_changes_fields_and_is_immutable_on_name(db) -> None:
    reg.create_token(
        db, name="edit_me", label="old", data_type="text", party="internal"
    )
    view = reg.update_meta(
        db,
        "edit_me",
        label="new label",
        help_text="new help",
        data_type="date",
        party="counterparty",
    )
    assert view.label == "new label"
    assert view.data_type == "date"
    assert view.party == "counterparty"
    # Name/placeholder are not update_meta parameters — the token identity is stable.
    assert view.name == "edit_me"
    assert view.placeholder == "{{edit_me}}"


def test_update_meta_missing_raises(db) -> None:
    with pytest.raises(reg.TokenNotFoundError):
        reg.update_meta(db, "no_such_token", label="x")


# --------------------------------------------------------------------------- #
# Delete usage-report matrix
# --------------------------------------------------------------------------- #
def test_delete_unused_token_proceeds_without_force(db) -> None:
    reg.create_token(db, name="lonely_token")
    result = reg.delete_token(db, "lonely_token")
    assert result.deleted is True
    assert result.usage.in_use is False
    assert reg.get_token(db, "lonely_token") is None


def test_delete_blocked_by_template_blob_usage(db) -> None:
    # A tokenised template whose .docx references {{counterparty_name}} (split-safe scan).
    _seed_template_blob(
        db,
        _make_docx(
            "This Agreement is with {{counterparty_name}}.", "{{effective_date}}"
        ),
    )
    reg.create_token(db, name="counterparty_name")

    report = reg.token_usage(db, "counterparty_name")
    assert len(report.template_versions) == 1
    tv_usage = report.template_versions[0]
    assert tv_usage.variant_code == "tokenised"
    assert tv_usage.is_current is True

    blocked = reg.delete_token(db, "counterparty_name", force=False)
    assert blocked.deleted is False
    assert blocked.usage.in_use is True
    # Still present — a blocked delete leaves the token untouched.
    assert reg.get_token(db, "counterparty_name") is not None

    forced = reg.delete_token(db, "counterparty_name", force=True)
    assert forced.deleted is True
    assert forced.forced is True
    assert reg.get_token(db, "counterparty_name") is None


def test_delete_scan_ignores_null_blobs(db) -> None:
    # A template_version with a NULL blob (unloaded seed template) must not crash / match the scan.
    tmpl = Template(
        org_id=DEFAULT_ORG_ID,
        jurisdiction_code="SG",
        counterparty_type_code="Company",
        mutuality_code="NotApplicable",
        name="SG Company NDA",
    )
    db.add(tmpl)
    db.flush()
    db.add(
        TemplateVersion(
            template_id=tmpl.id,
            variant_code="tokenised",
            version_no=1,
            blob_id=None,
            is_current=True,
        )
    )
    db.commit()
    reg.create_token(db, name="unused_scan_token")
    report = reg.token_usage(db, "unused_scan_token")
    assert report.template_versions == ()
    assert reg.delete_token(db, "unused_scan_token").deleted is True
