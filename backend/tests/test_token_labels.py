"""Seed-token human labels (issue 1 + the palette side of issue 3).

Covers :mod:`app.registry.seed_meta`: the curated ``token_registry_meta`` labels seeded on boot, the
insert-only idempotency (a re-run never clobbers an admin-edited label or duplicates a row), the
address trio (Street / City / Country), and the :func:`humanize_token_name` read-path fallback that
keeps any unlabeled token from surfacing raw snake_case. Zero network; the throwaway per-test SQLite DB
comes from ``conftest``.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.models_v2 import Token
from app.registry import tokens as reg
from app.registry.models import TokenMeta
from app.registry.seed_meta import TOKEN_META, humanize_token_name, seed_token_meta
from app.seed_catalog import TOKENS


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _insert_bare_tokens(db) -> None:
    """Insert the 16 seed ``token`` rows WITHOUT any meta — the state a freshly-seeded (or already
    deployed) DB is in before the label seeder runs."""
    for tk in TOKENS:
        db.add(
            Token(
                id=uuid.uuid4().hex,
                name=tk["name"],
                placeholder="{{" + tk["name"] + "}}",
                description=tk["description"],
                scope_code=tk["scope"],
            )
        )
    db.commit()


# --------------------------------------------------------------------------- #
# Curated metadata coverage
# --------------------------------------------------------------------------- #
def test_token_meta_covers_exactly_the_seed_tokens() -> None:
    # The curated table and the seed catalog must not drift apart.
    assert set(TOKEN_META) == {tk["name"] for tk in TOKENS}


def test_seed_populates_curated_labels(db) -> None:
    _insert_bare_tokens(db)
    seed_token_meta(db)
    db.commit()

    views = {v.name: v for v in reg.list_tokens(db)}
    assert views["counterparty_name"].label == "Counterparty Name"
    assert views["amperesand_signer_name"].label == "Amperesand Signer Name"
    assert views["purpose"].label == "Purpose"

    # data_type / party narrowing.
    assert views["notice_email"].data_type == "email"
    assert views["effective_date"].data_type == "date"
    assert views["counterparty_name"].party == "counterparty"
    assert views["effective_date"].party == "internal"


def test_every_seed_token_gets_a_clean_label(db) -> None:
    _insert_bare_tokens(db)
    seed_token_meta(db)
    db.commit()
    for tk in TOKENS:
        view = reg.get_token(db, tk["name"])
        # Never raw snake_case: a non-empty, Title-cased label with no underscores.
        assert view.label
        assert "_" not in view.label
        assert view.label != tk["name"]


# --------------------------------------------------------------------------- #
# Issue 1: address is three insertable tokens Street / City / Country
# --------------------------------------------------------------------------- #
def test_address_trio_labels(db) -> None:
    _insert_bare_tokens(db)
    seed_token_meta(db)
    db.commit()
    views = {v.name: v for v in reg.list_tokens(db)}
    assert views["street_address"].label == "Street"
    assert views["city_zip"].label == "City"
    assert views["country"].label == "Country"
    # The user-specified help copy for the city field.
    assert views["city_zip"].help_text == "City and postal/ZIP code"


# --------------------------------------------------------------------------- #
# Idempotency: insert-only, never clobbers an admin edit, never duplicates
# --------------------------------------------------------------------------- #
def test_seed_is_insert_only_and_does_not_clobber_edits(db) -> None:
    _insert_bare_tokens(db)
    seed_token_meta(db)
    db.commit()

    # Admin renames a label through the registry service.
    reg.update_meta(db, "counterparty_name", label="Party B (custom)")

    # Re-running the seeder must leave the edit intact and add no rows.
    seed_token_meta(db)
    db.commit()

    assert reg.get_token(db, "counterparty_name").label == "Party B (custom)"
    count = db.execute(sa.select(sa.func.count()).select_from(TokenMeta)).scalar_one()
    assert count == len(TOKENS)  # exactly one meta row per token, seeded once


def test_seed_skips_tokens_absent_from_db(db) -> None:
    # No token rows at all -> seeder is a no-op (no crash, no meta rows).
    seed_token_meta(db)
    db.commit()
    count = db.execute(sa.select(sa.func.count()).select_from(TokenMeta)).scalar_one()
    assert count == 0


# --------------------------------------------------------------------------- #
# Issue 3 (palette side): humanization fallback for an unlabeled token
# --------------------------------------------------------------------------- #
def test_humanize_token_name() -> None:
    assert humanize_token_name("street_address") == "Street Address"
    assert humanize_token_name("counterparty_name") == "Counterparty Name"
    assert humanize_token_name("notice_email") == "Notice Email"
    assert humanize_token_name("country") == "Country"


def test_view_falls_back_to_humanized_label_when_unlabeled(db) -> None:
    # A token with no meta row at all still yields a clean Title-Cased label (never {{snake_case}}).
    db.add(
        Token(
            id=uuid.uuid4().hex,
            name="some_new_field",
            placeholder="{{some_new_field}}",
            description="",
            scope_code="all",
        )
    )
    db.commit()
    view = reg.get_token(db, "some_new_field")
    assert view.label == "Some New Field"

    # And it shows up in list_tokens with the same clean label.
    listed = {v.name: v for v in reg.list_tokens(db)}
    assert listed["some_new_field"].label == "Some New Field"
