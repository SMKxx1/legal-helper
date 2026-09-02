"""Seed catalog — the 8 NDA templates, their empty/tokenised versions, the 16 scoped tokens, and the
derived token->template mapping (Deliverable #4).

DATA only + one idempotent apply function reused by both ``db.init_db`` (fresh dev/test DB) and
Alembic migration 0015. ``token_template`` is MATERIALIZED from each token's scope rule here — it is
the single relational mapping n8n's Code node and the Tally form query ("which fields does template X
need?"). Template/token *bytes* are not seeded (blob_id stays NULL): the files are re-uploaded.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa

from .schemas import DEFAULT_ORG_ID

# The 8 logical templates (jurisdiction × counterparty_type × mutuality). Mutuality applies ONLY to
# Individual; everything else is NotApplicable — matching the CHECK on ``template``.
TEMPLATES: list[dict] = [
    {
        "jurisdiction": "US",
        "counterparty_type": "ServiceProvider",
        "mutuality": "NotApplicable",
        "name": "US Service Provider NDA",
    },
    {
        "jurisdiction": "SG",
        "counterparty_type": "ServiceProvider",
        "mutuality": "NotApplicable",
        "name": "SG Service Provider NDA",
    },
    {
        "jurisdiction": "US",
        "counterparty_type": "Company",
        "mutuality": "NotApplicable",
        "name": "US Company NDA",
    },
    {
        "jurisdiction": "SG",
        "counterparty_type": "Company",
        "mutuality": "NotApplicable",
        "name": "SG Company NDA",
    },
    {
        "jurisdiction": "US",
        "counterparty_type": "Individual",
        "mutuality": "Mutual",
        "name": "US Individual NDA (Mutual)",
    },
    {
        "jurisdiction": "SG",
        "counterparty_type": "Individual",
        "mutuality": "Mutual",
        "name": "SG Individual NDA (Mutual)",
    },
    {
        "jurisdiction": "US",
        "counterparty_type": "Individual",
        "mutuality": "Unilateral",
        "name": "US Individual NDA (Unilateral)",
    },
    {
        "jurisdiction": "SG",
        "counterparty_type": "Individual",
        "mutuality": "Unilateral",
        "name": "SG Individual NDA (Unilateral)",
    },
]

# The 16 merge tokens. ``scope`` is a ref_token_scope code; the apply step expands it into
# token_template rows. ``name`` is stored bare; ``placeholder`` is the {{…}} form used in documents.
TOKENS: list[dict] = [
    {
        "name": "amperesand_signer_name",
        "scope": "all",
        "description": "Full name of the Amperesand signatory.",
    },
    {
        "name": "amperesand_signer_title",
        "scope": "all",
        "description": "Job title of the Amperesand signatory.",
    },
    {
        "name": "counterparty_name",
        "scope": "all",
        "description": "Legal name of the counterparty (entity or individual).",
    },
    {
        "name": "counterparty_signer_name",
        "scope": "company_sp",
        "description": "Full name of the counterparty's authorized signatory.",
    },
    {
        "name": "counterparty_signer_title",
        "scope": "company_sp",
        "description": "Job title of the counterparty's authorized signatory.",
    },
    {
        "name": "individual_id_number",
        "scope": "individual",
        "description": "Personal identification number of the individual counterparty.",
    },
    {
        "name": "counterparty_company_registration_number",
        "scope": "company_sp",
        "description": "Company / business registration number of the counterparty.",
    },
    {
        "name": "jurisdiction",
        "scope": "company_sp",
        "description": "Governing-law jurisdiction for the agreement.",
    },
    {
        "name": "street_address",
        "scope": "all",
        "description": "Street address of the counterparty's notice address.",
    },
    {
        "name": "city_zip",
        "scope": "all",
        "description": "City and postal/ZIP code of the notice address.",
    },
    {
        "name": "country",
        "scope": "all",
        "description": "Country of the notice address.",
    },
    {
        "name": "attn",
        "scope": "company_sp",
        "description": "Attention line / recipient for formal notices.",
    },
    {
        "name": "notice_email",
        "scope": "all",
        "description": "Email address for contractual notices.",
    },
    {
        "name": "effective_date",
        "scope": "all",
        "description": "Effective date of the agreement.",
    },
    {
        "name": "purpose",
        "scope": "mnda_only",
        "description": "Stated purpose for which confidential information is shared (mutual NDAs).",
    },
    {
        "name": "services",
        "scope": "sp_only",
        "description": "Description of the services the provider performs.",
    },
]

_VARIANTS = ("empty", "tokenised")


def template_matches_scope(scope: str, counterparty_type: str, mutuality: str) -> bool:
    """The scope rules from the brief, made executable. Derives token_template from token.scope."""
    if scope == "all":
        return True
    if scope == "company_sp":
        return counterparty_type in ("Company", "ServiceProvider")
    if scope == "individual":
        return counterparty_type == "Individual"
    if scope == "sp_only":
        return counterparty_type == "ServiceProvider"
    if scope == "mnda_only":
        return mutuality == "Mutual"
    return False


def seed_templates_tokens(conn, org_id: str = DEFAULT_ORG_ID) -> None:
    """Idempotently seed the 8 templates (+ empty/tokenised versions), the 16 tokens, and the derived
    token_template mapping. ``conn`` is any SQLAlchemy executor (Connection or Session)."""
    now = datetime.now(UTC)

    # 1. templates + their empty/tokenised versions ---------------------------------------------- #
    tmpl_ids: dict[tuple[str, str, str], str] = {}
    for t in TEMPLATES:
        key = (t["jurisdiction"], t["counterparty_type"], t["mutuality"])
        row = conn.execute(
            sa.text(
                "SELECT id FROM template WHERE jurisdiction_code=:j AND counterparty_type_code=:c "
                "AND mutuality_code=:m"
            ),
            {"j": key[0], "c": key[1], "m": key[2]},
        ).fetchone()
        if row:
            tid = row[0]
        else:
            tid = uuid.uuid4().hex
            conn.execute(
                sa.text(
                    "INSERT INTO template (id, org_id, jurisdiction_code, counterparty_type_code, "
                    "mutuality_code, name, description, active, created_at, updated_at) VALUES "
                    "(:id, :org, :j, :c, :m, :name, '', :active, :now, :now)"
                ),
                {
                    "id": tid,
                    "org": org_id,
                    "j": key[0],
                    "c": key[1],
                    "m": key[2],
                    "name": t["name"],
                    "active": True,
                    "now": now,
                },
            )
        tmpl_ids[key] = tid
        for variant in _VARIANTS:
            exists = conn.execute(
                sa.text(
                    "SELECT id FROM template_version WHERE template_id=:t AND variant_code=:v "
                    "AND version_no=1"
                ),
                {"t": tid, "v": variant},
            ).fetchone()
            if not exists:
                conn.execute(
                    sa.text(
                        "INSERT INTO template_version (id, template_id, variant_code, version_no, "
                        "blob_id, is_current, created_at) VALUES (:id, :t, :v, 1, NULL, :cur, :now)"
                    ),
                    {
                        "id": uuid.uuid4().hex,
                        "t": tid,
                        "v": variant,
                        "cur": True,
                        "now": now,
                    },
                )

    # 2. tokens ----------------------------------------------------------------------------------- #
    tok: dict[str, tuple[str, str]] = {}  # name -> (id, scope)
    for tk in TOKENS:
        row = conn.execute(
            sa.text("SELECT id FROM token WHERE name=:n"), {"n": tk["name"]}
        ).fetchone()
        if row:
            tid = row[0]
        else:
            tid = uuid.uuid4().hex
            conn.execute(
                sa.text(
                    "INSERT INTO token (id, name, placeholder, description, scope_code, created_at) "
                    "VALUES (:id, :n, :ph, :d, :s, :now)"
                ),
                {
                    "id": tid,
                    "n": tk["name"],
                    "ph": "{{" + tk["name"] + "}}",
                    "d": tk["description"],
                    "s": tk["scope"],
                    "now": now,
                },
            )
        tok[tk["name"]] = (tid, tk["scope"])

    # 3. token_template — materialize the scope rules -------------------------------------------- #
    for _name, (tid, scope) in tok.items():
        for (_j, c, m), tmpl_id in tmpl_ids.items():
            if not template_matches_scope(scope, c, m):
                continue
            exists = conn.execute(
                sa.text(
                    "SELECT 1 FROM token_template WHERE token_id=:tk AND template_id=:tp"
                ),
                {"tk": tid, "tp": tmpl_id},
            ).fetchone()
            if not exists:
                conn.execute(
                    sa.text(
                        "INSERT INTO token_template (token_id, template_id) VALUES (:tk, :tp)"
                    ),
                    {"tk": tid, "tp": tmpl_id},
                )

    # 4. token metadata — curated human labels / help / data_type / party -------------------------- #
    # Insert-only upsert into ``token_registry_meta`` so the palette shows "Street" not
    # ``{{street_address}}``. Same boot path as the tokens above, so a fresh DB and the already-deployed
    # Azure DB both get the labels; a re-run never clobbers an admin-edited label. (Lazy import keeps
    # the registry package out of this DATA module's import graph until seed time.)
    from .registry.seed_meta import seed_token_meta

    seed_token_meta(conn)
