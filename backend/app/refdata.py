"""Reference / lookup tables — the canonical enumerations as DATA (schema redesign, 3NF).

Every closed set in the domain (jurisdiction, counterparty_type, mutuality, all statuses, channels,
roles, review_type, intents, …) is a lookup table here instead of a bare ``varchar``. This gives
real FK integrity, lets a value carry extra columns the app needs (``sort_order`` / ``color`` /
``bucket`` / ``is_terminal`` — turning ``clm/derive.py``'s color/bucket maps into joinable data),
and lets the set be extended without an ``ALTER TYPE`` (native PG enums were the alternative;
rejected for the carry-extra-columns reason — see docs/schema-redesign/02-DESIGN.md §0).

``CATALOG`` is the single source of truth for the seed rows. The Alembic migration bulk-inserts it
on prod; ``seed_lookups()`` inserts it on a fresh ``create_all`` dev/test DB. Both are idempotent.
"""

from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


# --------------------------------------------------------------------------- #
# Base classes for the two lookup shapes.
# --------------------------------------------------------------------------- #
class _Lookup(Base):
    """Simple (code, label, sort_order) reference row."""

    __abstract__ = True

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    label: Mapped[str] = mapped_column(String(128), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


# --- plain lookups --------------------------------------------------------- #
class Jurisdiction(_Lookup):
    __tablename__ = "ref_jurisdiction"  # US | SG


class CounterpartyType(_Lookup):
    __tablename__ = "ref_counterparty_type"  # ServiceProvider | Company | Individual


class Mutuality(_Lookup):
    __tablename__ = "ref_mutuality"  # Mutual | Unilateral | NotApplicable


class TemplateVariant(_Lookup):
    __tablename__ = "ref_template_variant"  # empty | tokenised


# --- lookups that carry extra columns -------------------------------------- #
class TokenScope(_Lookup):
    __tablename__ = (
        "ref_token_scope"  # all | company_sp | individual | sp_only | mnda_only
    )

    description: Mapped[str] = mapped_column(String(255), default="")


# --------------------------------------------------------------------------- #
# CATALOG — the seed rows. {table_name: [row_dict, ...]}. Single source of truth.
# --------------------------------------------------------------------------- #
CATALOG: dict[str, list[dict]] = {
    "ref_jurisdiction": [
        {"code": "US", "label": "United States", "sort_order": 1},
        {"code": "SG", "label": "Singapore", "sort_order": 2},
    ],
    "ref_counterparty_type": [
        {"code": "ServiceProvider", "label": "Service Provider", "sort_order": 1},
        {"code": "Company", "label": "Company", "sort_order": 2},
        {"code": "Individual", "label": "Individual", "sort_order": 3},
    ],
    "ref_mutuality": [
        {"code": "Mutual", "label": "Mutual", "sort_order": 1},
        {"code": "Unilateral", "label": "Unilateral", "sort_order": 2},
        {"code": "NotApplicable", "label": "Not Applicable", "sort_order": 3},
    ],
    "ref_template_variant": [
        {"code": "empty", "label": "Empty (clean baseline)", "sort_order": 1},
        {"code": "tokenised", "label": "Tokenised ({{placeholders}})", "sort_order": 2},
    ],
    "ref_token_scope": [
        {
            "code": "all",
            "label": "All documents",
            "sort_order": 1,
            "description": "Appears in all 8 templates.",
        },
        {
            "code": "company_sp",
            "label": "Companies & Service Providers",
            "sort_order": 2,
            "description": "counterparty_type IN (Company, ServiceProvider).",
        },
        {
            "code": "individual",
            "label": "Individual",
            "sort_order": 3,
            "description": "counterparty_type = Individual.",
        },
        {
            "code": "sp_only",
            "label": "Service Providers only",
            "sort_order": 4,
            "description": "counterparty_type = ServiceProvider.",
        },
        {
            "code": "mnda_only",
            "label": "Mutual NDAs only",
            "sort_order": 5,
            "description": "mutuality = Mutual.",
        },
    ],
}


def seed_lookups(session) -> None:
    """Idempotently insert any missing CATALOG rows (fresh create_all dev/test DBs)."""
    # Resolve table -> mapped class once.
    classes = {
        c.class_.__tablename__: c.class_
        for c in Base.registry.mappers
        if c.class_.__tablename__ in CATALOG
    }
    for tname, rows in CATALOG.items():
        cls = classes.get(tname)
        if cls is None:
            continue
        for row in rows:
            if session.get(cls, row["code"]) is None:
                session.add(cls(**row))
