"""Token-registry CRUD service (PLAN §3.7 — "Token registry (user-managed)").

The 16 seed tokens already live as ``models_v2.Token`` rows (seeded by ``app.seed_catalog``). This
service turns that seed catalog into a **user-managed registry**: admins create tokens freely, edit
their metadata, and delete them — but a delete first shows *every usage* (template versions whose .docx
contains ``{{name}}`` + form blocks bound to it) and refuses unless ``force=True``.

It writes across two tables in lock-step so ported code that reads ``Token`` never has to change:

* ``token`` (:class:`app.models_v2.Token`) — the canonical row (``name`` / ``placeholder`` /
  ``scope_code`` / ``description``). Its schema is untouched.
* ``token_registry_meta`` (:class:`app.registry.models.TokenMeta`) — the additive companion holding the
  user-facing ``label`` / ``help_text`` / ``data_type`` / ``party`` / ``fallback_text``.
* ``token_template`` (:class:`app.models_v2.TokenTemplate`) — materialized from the token's
  ``scope_code`` on create (reusing ``seed_catalog.template_matches_scope``), so the ported
  "which fields does template X need?" query stays correct for user-created tokens too. A no-op when no
  templates are seeded yet.

Every create/delete returns cleanly and emits a **drift event** via :mod:`app.registry.drift` (the
caller passes the notifier); studio/publish code calls the drift emit hooks directly for template
changes. Names are validated snake_case and unique across ``token.name`` AND ``token.placeholder``.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models_v2 import DocumentBlob, Template, TemplateVersion, Token, TokenTemplate
from ..telemetry import get_logger
from .docx_scan import scan_docx_tokens
from .models import DATA_TYPES, PARTIES, TokenMeta
from .seed_meta import humanize_token_name

log = get_logger("nda.registry.tokens")

#: Proper snake_case: lowercase alnum segments joined by single underscores, starting with a letter.
#: Matches the 16 seed tokens (``amperesand_signer_name``, ``city_zip`` …) and rejects leading/trailing
#: or doubled underscores, capitals, spaces, and ``{{…}}`` braces.
_SNAKE_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")

#: Default scope for a user-created token when the caller does not pin one — appears in all templates.
DEFAULT_SCOPE_CODE = "all"


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Errors (typed so the wave-B admin UI + API can map them to friendly messages)
# --------------------------------------------------------------------------- #
class TokenRegistryError(Exception):
    """Base class for token-registry validation / state errors."""


class TokenValidationError(TokenRegistryError):
    """A name / data_type / party value the registry refuses (with a plain-English message)."""


class TokenExistsError(TokenRegistryError):
    """A create whose ``name`` (or derived ``{{placeholder}}``) already exists."""


class TokenNotFoundError(TokenRegistryError):
    """An update/delete/lookup for a token that is not in the registry."""


# --------------------------------------------------------------------------- #
# Usage report (the delete-safety matrix: blob scan + form bindings)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TemplateUsage:
    """One template version whose current .docx references ``{{token}}``."""

    template_version_id: str
    template_id: str
    variant_code: str
    version_no: int
    is_current: bool
    template_name: str = ""


@dataclass(frozen=True)
class FormBindingUsage:
    """One form that binds the token in a block (draft and/or published)."""

    form_id: str
    form_name: str
    kind: str
    block_ids: tuple[str, ...]
    in_draft: bool
    in_published: bool


@dataclass(frozen=True)
class UsageReport:
    """Everything that would break if the token were removed (PLAN §3.7 "deletion shows every usage")."""

    token_id: str
    token_name: str
    template_versions: tuple[TemplateUsage, ...] = ()
    form_bindings: tuple[FormBindingUsage, ...] = ()

    @property
    def in_use(self) -> bool:
        return bool(self.template_versions or self.form_bindings)


@dataclass(frozen=True)
class DeleteResult:
    """Outcome of :func:`delete_token`. ``deleted`` is False when usage was found and ``force`` was not
    set — the token is untouched and ``usage`` explains why. ``usage`` is always populated."""

    deleted: bool
    usage: UsageReport
    forced: bool = False


@dataclass
class TokenView:
    """A registry token joined with its metadata — the read shape the service returns/lists."""

    id: str
    name: str
    placeholder: str
    scope_code: str
    label: str = ""
    help_text: str = ""
    data_type: str = "text"
    party: str = "internal"
    fallback_text: str = ""
    description: str = ""
    created_by: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_name(name: str) -> str:
    """Return the validated snake_case token name, or raise :class:`TokenValidationError`."""
    n = (name or "").strip()
    if not n:
        raise TokenValidationError("Token name is required.")
    if not _SNAKE_RE.match(n):
        raise TokenValidationError(
            f"Invalid token name {name!r}. Use lowercase snake_case: a letter, then letters/digits "
            "joined by single underscores (e.g. counterparty_signer_name)."
        )
    if len(n) > 64:
        raise TokenValidationError("Token name must be at most 64 characters.")
    return n


def _validate_data_type(data_type: str) -> str:
    dt = (data_type or "").strip().lower()
    if dt not in DATA_TYPES:
        raise TokenValidationError(
            f"Invalid data_type {data_type!r}. One of: {', '.join(DATA_TYPES)}."
        )
    return dt


def _validate_party(party: str) -> str:
    p = (party or "").strip().lower()
    if p not in PARTIES:
        raise TokenValidationError(
            f"Invalid party {party!r}. One of: {', '.join(PARTIES)}."
        )
    return p


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def _get_token(db: Session, name_or_id: str) -> Token | None:
    """Resolve a token by primary-key id first, then by name."""
    row = db.get(Token, name_or_id)
    if row is not None:
        return row
    return db.execute(
        select(Token).where(Token.name == name_or_id)
    ).scalar_one_or_none()


def get_token(db: Session, name_or_id: str) -> TokenView | None:
    """A joined :class:`TokenView` for a token (by id or name), or ``None`` if absent."""
    tok = _get_token(db, name_or_id)
    if tok is None:
        return None
    meta = db.get(TokenMeta, tok.id)
    return _view(tok, meta)


def list_tokens(db: Session) -> list[TokenView]:
    """Every registry token joined with its metadata, ordered by name."""
    tokens = db.execute(select(Token).order_by(Token.name)).scalars().all()
    metas = {m.token_id: m for m in db.execute(select(TokenMeta)).scalars().all()}
    return [_view(t, metas.get(t.id)) for t in tokens]


def registry_token_names(db: Session) -> set[str]:
    """The set of live token names — the desired binding set a drift sync plan targets."""
    return set(db.execute(select(Token.name)).scalars().all())


def _view(tok: Token, meta: TokenMeta | None) -> TokenView:
    # Belt-and-braces: a token with no meta row (or a blank label) still gets a Title-Cased label
    # derived from its name, so neither the palette nor the document view ever shows raw snake_case.
    label = (meta.label if meta else "") or ""
    return TokenView(
        id=tok.id,
        name=tok.name,
        placeholder=tok.placeholder,
        scope_code=tok.scope_code,
        label=label or humanize_token_name(tok.name),
        help_text=(meta.help_text if meta else "") or "",
        data_type=(meta.data_type if meta else "text") or "text",
        party=(meta.party if meta else "internal") or "internal",
        fallback_text=(meta.fallback_text if meta else "") or "",
        description=tok.description or "",
        created_by=(meta.created_by if meta else None),
    )


# --------------------------------------------------------------------------- #
# token_template materialization (reuses the seed-catalog scope rules)
# --------------------------------------------------------------------------- #
def _materialize_token_template(db: Session, token_id: str, scope_code: str) -> int:
    """Insert ``token_template`` rows for every seeded template the scope covers (idempotent). Returns
    the number of rows added. A no-op when no templates are seeded yet (fresh test DB)."""
    from ..seed_catalog import template_matches_scope

    templates = db.execute(select(Template)).scalars().all()
    added = 0
    for tmpl in templates:
        if not template_matches_scope(
            scope_code, tmpl.counterparty_type_code, tmpl.mutuality_code
        ):
            continue
        existing = db.execute(
            select(TokenTemplate).where(
                TokenTemplate.token_id == token_id,
                TokenTemplate.template_id == tmpl.id,
            )
        ).first()
        if existing is None:
            db.add(TokenTemplate(token_id=token_id, template_id=tmpl.id))
            added += 1
    return added


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #
def create_token(
    db: Session,
    *,
    name: str,
    label: str = "",
    help_text: str = "",
    data_type: str = "text",
    party: str = "internal",
    fallback_text: str = "",
    scope_code: str = DEFAULT_SCOPE_CODE,
    created_by: str | None = None,
    link_templates: bool = True,
    notifier: Any | None = None,
) -> TokenView:
    """Create a registry token + its metadata (PLAN §3.7). Validates the snake_case name and enforces
    uniqueness on both ``token.name`` and ``token.placeholder``. Materializes ``token_template`` rows
    from ``scope_code`` (unless ``link_templates=False``) and emits a ``token_created`` drift event.

    ``notifier`` (a :class:`app.registry.drift.DriftNotifier`) is forwarded to the drift emit so form
    owners are told to sync; pass ``None`` to flag forms without notifying.
    """
    n = validate_name(name)
    dt = _validate_data_type(data_type)
    pty = _validate_party(party)
    placeholder = "{{" + n + "}}"

    clash = db.execute(
        select(Token).where((Token.name == n) | (Token.placeholder == placeholder))
    ).scalar_one_or_none()
    if clash is not None:
        raise TokenExistsError(f"A token named {n!r} already exists.")

    token = Token(
        id=uuid.uuid4().hex,
        name=n,
        placeholder=placeholder,
        # Keep the ported ``description`` populated from help_text so code reading Token.description
        # (the generator's expected-value hint) still gets something; help_text stays authoritative.
        description=help_text or "",
        scope_code=scope_code,
    )
    db.add(token)
    db.flush()  # allocate token.id for the meta FK + token_template rows
    db.add(
        TokenMeta(
            token_id=token.id,
            label=label or "",
            help_text=help_text or "",
            data_type=dt,
            party=pty,
            fallback_text=fallback_text or "",
            created_by=created_by,
        )
    )
    if link_templates:
        _materialize_token_template(db, token.id, scope_code)
    db.commit()
    db.refresh(token)
    log.info(
        "registry.token.created",
        token_id=token.id,
        name=n,
        data_type=dt,
        party=pty,
        scope_code=scope_code,
        created_by=created_by,
    )

    # Drift: a new token means NDA forms may want a field for it (PLAN §3.7 drift → notify → sync).
    from .drift import emit_token_created

    emit_token_created(db, n, notifier=notifier)

    meta = db.get(TokenMeta, token.id)
    return _view(token, meta)


# --------------------------------------------------------------------------- #
# Update (metadata only — name/placeholder are immutable so bound docx/forms stay valid)
# --------------------------------------------------------------------------- #
def update_meta(
    db: Session,
    name_or_id: str,
    *,
    label: str | None = None,
    help_text: str | None = None,
    data_type: str | None = None,
    party: str | None = None,
    fallback_text: str | None = None,
) -> TokenView:
    """Update a token's metadata in place (PLAN §3.7). Only the provided fields change; the token
    ``name`` / ``placeholder`` are intentionally immutable (renaming would orphan every ``{{name}}`` in
    stored docx + every form binding — that is a delete-and-recreate, surfaced through the usage report).
    Creates the meta row if a legacy/seed token had none. Raises :class:`TokenNotFoundError`."""
    tok = _get_token(db, name_or_id)
    if tok is None:
        raise TokenNotFoundError(f"No such token: {name_or_id!r}.")

    dt = _validate_data_type(data_type) if data_type is not None else None
    pty = _validate_party(party) if party is not None else None

    meta = db.get(TokenMeta, tok.id)
    if meta is None:
        meta = TokenMeta(token_id=tok.id)
        db.add(meta)
    if label is not None:
        meta.label = label
    if help_text is not None:
        meta.help_text = help_text
        tok.description = help_text  # keep the ported Token.description mirror in sync
    if dt is not None:
        meta.data_type = dt
    if pty is not None:
        meta.party = pty
    if fallback_text is not None:
        meta.fallback_text = fallback_text
    meta.updated_at = _now()
    db.commit()
    db.refresh(tok)
    meta = db.get(TokenMeta, tok.id)
    log.info("registry.token.updated", token_id=tok.id, name=tok.name)
    return _view(tok, meta)


# --------------------------------------------------------------------------- #
# Usage report (delete safety)
# --------------------------------------------------------------------------- #
def token_usage(db: Session, name_or_id: str) -> UsageReport:
    """Build the full usage report for a token (PLAN §3.7 delete-safety): every template version whose
    current .docx contains ``{{name}}`` (scanned from the stored blob) + every form that binds it.

    Read-only; safe to call independently of a delete (the wave-B UI shows it before confirming)."""
    tok = _get_token(db, name_or_id)
    if tok is None:
        raise TokenNotFoundError(f"No such token: {name_or_id!r}.")
    return UsageReport(
        token_id=tok.id,
        token_name=tok.name,
        template_versions=_scan_template_usage(db, tok.name),
        form_bindings=_scan_form_usage(db, tok.name),
    )


def _scan_template_usage(db: Session, name: str) -> tuple[TemplateUsage, ...]:
    """Every ``template_version`` with a loaded .docx blob that references ``{{name}}``."""
    rows = db.execute(
        select(TemplateVersion, DocumentBlob, Template)
        .join(DocumentBlob, TemplateVersion.blob_id == DocumentBlob.id)
        .join(Template, Template.id == TemplateVersion.template_id)
        .where(DocumentBlob.bytes.is_not(None))
    ).all()
    hits: list[TemplateUsage] = []
    for tv, blob, tmpl in rows:
        if name in scan_docx_tokens(blob.bytes):
            hits.append(
                TemplateUsage(
                    template_version_id=tv.id,
                    template_id=tv.template_id,
                    variant_code=tv.variant_code,
                    version_no=tv.version_no,
                    is_current=bool(tv.is_current),
                    template_name=tmpl.name or "",
                )
            )
    return tuple(hits)


def _scan_form_usage(_db: Session, _name: str) -> tuple[FormBindingUsage, ...]:
    """Retired no-op: intake moved to the external Tally form, so there are no in-house forms binding
    tokens. Kept (returning empty) so the token-usage report + delete-guard shape stays stable."""
    return ()


# --------------------------------------------------------------------------- #
# Delete (usage-gated; force required to proceed while in use)
# --------------------------------------------------------------------------- #
def delete_token(
    db: Session,
    name_or_id: str,
    *,
    force: bool = False,
    notifier: Any | None = None,
) -> DeleteResult:
    """Delete a token — but only after showing its usage (PLAN §3.7).

    Always builds the :class:`UsageReport` first. If the token is in use and ``force`` is not set, the
    token is **left untouched** and ``DeleteResult(deleted=False, usage=…)`` is returned so the caller
    can surface the consequences and re-invoke with ``force=True``. When deleted (unused, or forced),
    the CASCADE FK removes its ``token_registry_meta`` + ``token_template`` rows and a ``token_deleted``
    drift event flags/notifies every affected form.
    """
    tok = _get_token(db, name_or_id)
    if tok is None:
        raise TokenNotFoundError(f"No such token: {name_or_id!r}.")
    name = tok.name
    usage = UsageReport(
        token_id=tok.id,
        token_name=name,
        template_versions=_scan_template_usage(db, name),
        form_bindings=_scan_form_usage(db, name),
    )

    if usage.in_use and not force:
        log.info(
            "registry.token.delete_blocked",
            token_id=tok.id,
            name=name,
            template_versions=len(usage.template_versions),
            form_bindings=len(usage.form_bindings),
        )
        return DeleteResult(deleted=False, usage=usage, forced=False)

    # Some SQLite builds ignore ondelete=CASCADE unless PRAGMA foreign_keys is ON (it is, per app.db).
    # Delete the companion rows explicitly too, so the delete is correct on any backend.
    db.execute(delete(TokenTemplate).where(TokenTemplate.token_id == tok.id))
    db.execute(delete(TokenMeta).where(TokenMeta.token_id == tok.id))
    db.delete(tok)
    db.commit()
    log.info(
        "registry.token.deleted",
        token_id=usage.token_id,
        name=name,
        forced=force and usage.in_use,
        template_versions=len(usage.template_versions),
        form_bindings=len(usage.form_bindings),
    )

    from .drift import emit_token_deleted

    emit_token_deleted(db, name, notifier=notifier)

    return DeleteResult(deleted=True, usage=usage, forced=force and usage.in_use)


__all__ = [
    "DATA_TYPES",
    "PARTIES",
    "DEFAULT_SCOPE_CODE",
    "TokenRegistryError",
    "TokenValidationError",
    "TokenExistsError",
    "TokenNotFoundError",
    "TemplateUsage",
    "FormBindingUsage",
    "UsageReport",
    "DeleteResult",
    "TokenView",
    "validate_name",
    "get_token",
    "list_tokens",
    "registry_token_names",
    "create_token",
    "update_meta",
    "token_usage",
    "delete_token",
]
