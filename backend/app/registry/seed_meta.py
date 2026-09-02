"""Curated human labels for the 16 seed tokens + the boot-time seeder that writes them.

The seed catalog (:mod:`app.seed_catalog`) creates the 16 ``token`` rows but only carries a bare
snake_case ``name`` and a machine ``description`` — so a fresh palette shows ``{{street_address}}``
instead of a friendly "Street". This module supplies the curated presentation metadata (label / help /
data_type / party) and an **insert-only** seeder that materializes it into ``token_registry_meta``
(:class:`app.registry.models.TokenMeta`).

Wiring: :func:`seed_token_meta` runs inside ``seed_catalog.seed_templates_tokens`` — the SAME
idempotent boot path (``db.init_db`` / Alembic-fronted deploy) that seeds the tokens themselves. So a
fresh dev/test DB *and* the already-deployed Azure DB (on its next boot) both pick up the labels.

Idempotency contract: the seeder INSERTs a meta row only when one is **absent**. An admin who later
renames a label through the registry UI keeps their edit — re-running the seeder never clobbers or
duplicates an existing row. :func:`humanize_token_name` is the belt-and-braces fallback the read path
(:func:`app.registry.tokens._view`) uses so no token can ever surface raw snake_case even without a
curated entry here.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

#: Curated metadata for each of the 16 seed tokens, keyed by ``token.name``. ``party`` is
#: ``counterparty`` for anything supplied by / describing the counterparty (its name, signatory, ID,
#: registration number, and its notice address), ``internal`` for Amperesand-side and agreement-level
#: fields. ``data_type`` narrows ``notice_email`` -> email and ``effective_date`` -> date; the rest are
#: free ``text``. ``fallback_text`` is left empty here so it never silently overrides an unfilled field.
TOKEN_META: dict[str, dict[str, str]] = {
    "amperesand_signer_name": {
        "label": "Amperesand Signer Name",
        "help_text": "Full name of the Amperesand signatory.",
        "data_type": "text",
        "party": "internal",
    },
    "amperesand_signer_title": {
        "label": "Amperesand Signer Title",
        "help_text": "Job title of the Amperesand signatory.",
        "data_type": "text",
        "party": "internal",
    },
    "counterparty_name": {
        "label": "Counterparty Name",
        "help_text": "Legal name of the counterparty (entity or individual).",
        "data_type": "text",
        "party": "counterparty",
    },
    "counterparty_signer_name": {
        "label": "Counterparty Signer Name",
        "help_text": "Full name of the counterparty's authorized signatory.",
        "data_type": "text",
        "party": "counterparty",
    },
    "counterparty_signer_title": {
        "label": "Counterparty Signer Title",
        "help_text": "Job title of the counterparty's authorized signatory.",
        "data_type": "text",
        "party": "counterparty",
    },
    "individual_id_number": {
        "label": "Individual ID Number",
        "help_text": "Personal identification number of the individual counterparty.",
        "data_type": "text",
        "party": "counterparty",
    },
    "counterparty_company_registration_number": {
        "label": "Company Registration Number",
        "help_text": "Company or business registration number of the counterparty.",
        "data_type": "text",
        "party": "counterparty",
    },
    "jurisdiction": {
        "label": "Jurisdiction",
        "help_text": "Governing-law jurisdiction for the agreement.",
        "data_type": "text",
        "party": "internal",
    },
    "street_address": {
        "label": "Street",
        "help_text": "Street address of the counterparty's notice address.",
        "data_type": "text",
        "party": "counterparty",
    },
    "city_zip": {
        "label": "City",
        "help_text": "City and postal/ZIP code",
        "data_type": "text",
        "party": "counterparty",
    },
    "country": {
        "label": "Country",
        "help_text": "Country of the counterparty's notice address.",
        "data_type": "text",
        "party": "counterparty",
    },
    "attn": {
        "label": "Attention",
        "help_text": "Attention line / recipient for formal notices.",
        "data_type": "text",
        "party": "counterparty",
    },
    "notice_email": {
        "label": "Notice Email",
        "help_text": "Email address for contractual notices.",
        "data_type": "email",
        "party": "counterparty",
    },
    "effective_date": {
        "label": "Effective Date",
        "help_text": "Effective date of the agreement.",
        "data_type": "date",
        "party": "internal",
    },
    "purpose": {
        "label": "Purpose",
        "help_text": "Stated purpose for which confidential information is shared (mutual NDAs).",
        "data_type": "text",
        "party": "internal",
    },
    "services": {
        "label": "Services",
        "help_text": "Description of the services the provider performs.",
        "data_type": "text",
        "party": "internal",
    },
}


def humanize_token_name(name: str) -> str:
    """Derive a Title-Cased label from a snake_case token name.

    ``street_address`` -> ``"Street Address"``. The belt-and-braces fallback for the read path so a
    token that has no curated/seeded label never surfaces raw snake_case in the palette or the document.
    """
    return " ".join(part.capitalize() for part in (name or "").split("_") if part)


def seed_token_meta(conn) -> None:
    """Idempotently seed ``token_registry_meta`` labels for the known seed tokens.

    Insert-only: for each name in :data:`TOKEN_META`, resolve its ``token.id`` and INSERT a meta row
    **only if one does not already exist** — so a re-run (fresh boot, or the already-deployed Azure DB
    on its next boot) never duplicates a row nor clobbers a label an admin edited through the registry
    UI. Tokens absent from the DB (not yet seeded) are skipped. ``conn`` is any SQLAlchemy executor
    (Connection or Session), matching ``seed_catalog.seed_templates_tokens``.
    """
    now = datetime.now(UTC)
    for name, meta in TOKEN_META.items():
        row = conn.execute(
            sa.text("SELECT id FROM token WHERE name=:n"), {"n": name}
        ).fetchone()
        if row is None:
            continue
        token_id = row[0]
        exists = conn.execute(
            sa.text("SELECT 1 FROM token_registry_meta WHERE token_id=:t"),
            {"t": token_id},
        ).fetchone()
        if exists:
            continue
        conn.execute(
            sa.text(
                "INSERT INTO token_registry_meta "
                "(token_id, label, help_text, data_type, party, fallback_text, "
                "created_at, updated_at) VALUES "
                "(:t, :label, :help, :dt, :party, :fb, :now, :now)"
            ),
            {
                "t": token_id,
                "label": meta["label"],
                "help": meta["help_text"],
                "dt": meta["data_type"],
                "party": meta["party"],
                "fb": meta.get("fallback_text", ""),
                "now": now,
            },
        )


__all__ = ["TOKEN_META", "humanize_token_name", "seed_token_meta"]
