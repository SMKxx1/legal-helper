"""ORM models: Template, Review, Issue, WebhookEvent (the LEGACY layer).

Status/severity/change-type values are stored as plain strings; the canonical
enumerations live in `schemas.py` so the API layer and DB stay in sync.

CANONICAL-LAYER NOTE: the project is mid additive cutover to the normalized 3NF schema in
``models_v2.py`` — **new tables/models should go there, not here.** This module stays live only where
the cutover hasn't reached. See ``docs/schema-redesign/02-DESIGN.md``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from . import (
    models_bot as _models_bot,  # noqa: E402,F401  (n8n NDA-bot integration tables)
)
from . import (
    models_v2 as _models_v2,  # noqa: E402,F401  (blob/party/template/token/sign/channel)
)
from . import refdata as _refdata  # noqa: E402,F401  (lookup/reference tables)

# Schema-redesign tables (3NF). Imported here so a fresh ``create_all`` (and Alembic autogenerate via
# ``alembic/env.py``) registers every new table on ``Base.metadata``. See docs/schema-redesign/.
from .archive import (
    models as _archive_models,  # noqa: E402,F401  (P4 archive: nda_cache_processed watcher ledger)
)
from .bot import (
    models as _bot_models,  # noqa: E402,F401  (P2 bot-core: inbox/allowlist/pending/correlation)
)
from .db import JSON_VARIANT, Base
from .registry import (
    models as _registry_models,  # noqa: E402,F401  (P5 token registry: token_registry_meta companion)
)
from .schemas import DEFAULT_ORG_ID
from .studio import (
    models as _studio_models,  # noqa: E402,F401  (P5 template studio: studio_ops undo/redo trail)
)


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class BaselineTemplate(Base):
    """A standard NDA template used as the comparison baseline (the legacy ``templates`` table).

    Named ``BaselineTemplate`` (not ``Template``) to avoid a mapper-registry collision with the
    NDA-generation ``models_v2.Template`` — two mapped classes sharing a name make any string-form
    ``relationship("Template")`` ambiguous. The table name is unchanged, so no migration is needed.
    """

    __tablename__ = "templates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    filename: Mapped[str] = mapped_column(String(512))
    storage_path: Mapped[str] = mapped_column(String(1024))
    file_format: Mapped[str] = mapped_column(String(16))  # docx | pdf | txt
    is_default: Mapped[bool] = mapped_column(default=False)
    clause_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AppSetting(Base):
    """Runtime configuration overrides (key/value), editable from the UI.

    These override the env-derived defaults in `config.Settings` so the user can
    switch provider/model or set an API key without restarting. See
    `app/settings_store.py` for the merge logic.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# --------------------------------------------------------------------------- #
# /v1 engine persistence (ARCHITECTURE §4: Contract / Event, review-centric).
# Stored separately from the legacy ``reviews`` table so the two never collide.
# Works on SQLite (dev) and Postgres (prod) via the same SQLAlchemy models.
# --------------------------------------------------------------------------- #
class Contract(Base):
    """The thing under review (an NDA), grouping its reviews over time (light-CLM).

    Full Party modelling is future; the counterparty name is kept inline for now.
    """

    __tablename__ = "contracts"
    # Composite index for the nightly expiry scan and the dashboard filters (status + expiry window).
    __table_args__ = (
        Index(
            "ix_contracts_lifecycle_expiration", "lifecycle_status", "expiration_date"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # Tenant scope — carried on every contract from day one (single default org for now). NOT NULL
    # with a server default so the P0-3 migration backfills existing rows without a table rebuild.
    org_id: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DEFAULT_ORG_ID,
        server_default=DEFAULT_ORG_ID,
        index=True,
    )
    contract_type: Mapped[str] = mapped_column(String(32), default="nda")
    title: Mapped[str] = mapped_column(String(512), default="")
    counterparty_name: Mapped[str] = mapped_column(String(255), default="")
    # Document identity used to group re-reviews. INDEXED (for the dedup lookup) but NOT unique —
    # migration 0002 relaxed the UNIQUE so one CLM contract can hold many distinct document
    # versions that may share identical bytes. Dedup is now best-effort (most-recent match), see
    # reviews_repo._get_or_create_contract.
    doc_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    # --- CLM lifecycle (Phase 0.5) ------------------------------------------------------------- #
    # The canonical state machine column (app/clm/lifecycle.py). Stored states only:
    # draft -> under_review -> negotiation -> ready_for_signature -> out_for_signature -> executed
    # -> {expired | renewed | terminated | void}. 'active'/'expiring' are DERIVED (derive.py), never
    # stored. NOT NULL with a server default so the migration backfills existing rows in place.
    lifecycle_status: Mapped[str] = mapped_column(
        String(32), default="draft", server_default="draft", nullable=False, index=True
    )
    # Optimistic-concurrency token: every successful transition bumps it; a stale write is rejected.
    lifecycle_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    lifecycle_status_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    owner_user_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    effective_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    execution_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expiration_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notice_period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auto_renew: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    renewal_term_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    renewal_terms_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default="{}", nullable=False
    )
    docusign_envelope_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    latest_risk_tier: Mapped[str] = mapped_column(
        String(16), default="", server_default="", nullable=False
    )
    latest_adherence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_reminder_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    reviews: Mapped[list[EngineReview]] = relationship(
        back_populates="contract",
        cascade="all, delete-orphan",
        foreign_keys="EngineReview.contract_id",
    )
    events: Mapped[list[ReviewEvent]] = relationship(
        back_populates="contract", cascade="all, delete-orphan"
    )


class EngineReview(Base):
    """A persisted /v1 engine review: the full structured payload + indexed metadata."""

    __tablename__ = "engine_reviews"
    # Composite index for contract-scoped history (ORDER BY created_at within a contract).
    __table_args__ = (
        Index("ix_engine_reviews_contract_created", "contract_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    contract_id: Mapped[str | None] = mapped_column(
        ForeignKey("contracts.id"), nullable=True, index=True
    )
    # Tenant scope (PL-7) — denormalized from the parent contract so org-scoped queries (cost rollup,
    # history) filter directly without a join; defaults to the bootstrap org. Rows with no contract
    # keep the default. Set explicitly on insert (PL-8).
    org_id: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DEFAULT_ORG_ID,
        server_default=DEFAULT_ORG_ID,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )
    source_channel: Mapped[str] = mapped_column(String(32), default="api", index=True)
    # The principal this review is attributed to (a UserAccount id, or a service-account/Slack/email
    # principal id once the engine allow-list lands in Phase 1). Nullable for legacy/unattributed rows.
    actor_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), default="deep")
    playbook_version: Mapped[str] = mapped_column(String(32), default="")
    perspective: Mapped[str] = mapped_column(String(32), default="")
    risk_tier: Mapped[str] = mapped_column(String(16), default="", index=True)
    adherence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    doc_filename: Mapped[str] = mapped_column(String(512), default="")
    doc_sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    # Document-reuse cache key: sha256 of the NORMALIZED text (app.engine.simcache).
    # Matches the same document across channels even when the raw bytes differ. See
    # reviews_repo.find_similar_review.
    norm_sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    # Playbook RELEASE that graded this review (app.playbook.release.playbook_release_id) — the
    # content-cache version key (audit #3). Both cache tiers filter on it so a review is only reused
    # by the SAME release; NULL on legacy rows never matches (falls through to a fresh review).
    playbook_release: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Full structured review result. ``jsonb`` on Postgres (queryable/indexable), JSON on SQLite;
    # round-trips as a native dict — callers store/read the dict directly (no json.dumps/loads).
    payload_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)

    contract: Mapped[Contract | None] = relationship(
        back_populates="reviews", foreign_keys="EngineReview.contract_id"
    )


class EngineReviewJob(Base):
    """An ASYNC /v1 review job (3.1 hardening — the one code addition CONTRACT.md pre-authorized).

    Sync ``/v1/reviews`` holds an n8n execution slot for the full engine wall-clock (worst case
    several minutes of provider retries). ``POST /v1/reviews?async=1`` instead persists the
    ALREADY-VALIDATED, ALREADY-EXTRACTED work here (202 + job_id) and the worker's claimer runs
    it; n8n POLLS ``GET /v1/reviews/jobs/{id}`` (the backend never calls n8n outbound).

    Crash-safety is the visibility-timeout pattern: a claim sets ``status='running'`` +
    ``lease_expires_at``; a worker that dies mid-run leaves an expired lease and the claimer
    re-claims the row (``attempts`` capped -> dead-letter ``failed``). The claim is the same
    atomic conditional-UPDATE shape as ``bot_dal.consume_request``. ``EngineReview`` itself has
    no status column — completed jobs point at the persisted review via ``review_id``.
    """

    __tablename__ = "engine_review_jobs"
    __table_args__ = (
        # The claimer's scan: pending rows (or expired running leases), oldest first.
        Index("ix_engine_review_jobs_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DEFAULT_ORG_ID,
        server_default=DEFAULT_ORG_ID,
        index=True,
    )
    principal_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )  # pending | running | done | failed
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # On a PENDING row: an optional retry deferral (not claimable before it). On a RUNNING row:
    # the visibility timeout (an expired lease means the claimer died — re-claimable).
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Fencing token, fresh per claim: complete/fail writes require the CURRENT token, so a
    # zombie run whose lease expired (job re-claimed elsewhere) can never clobber job state.
    claim_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The validated request, captured at submit time (text ALREADY extracted — the worker
    # never re-parses the upload; no /data file involved, so multi-replica-safe by design).
    mode: Mapped[str] = mapped_column(String(16), default="deep")
    scope: Mapped[str] = mapped_column(String(16), default="whole")
    playbook_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_channel: Mapped[str] = mapped_column(String(32), default="api")
    doc_filename: Mapped[str] = mapped_column(String(512), default="")
    doc_sha256: Mapped[str] = mapped_column(String(64), default="")
    norm_sha256: Mapped[str] = mapped_column(String(64), default="")
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    incoming_text: Mapped[str] = mapped_column(Text, default="")
    original_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Outcome
    review_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class ReviewEvent(Base):
    """Audit trail: an action taken on a contract / review (created, retrieved, redline)."""

    __tablename__ = "review_events"
    __table_args__ = (
        # Contract-timeline hot path: filter by contract_id, order by created_at DESC (routes_clm
        # detail view). Mirrors the composite on contract_status_history so the read is an index scan,
        # not a per-contract filesort.
        Index("ix_review_events_contract_created", "contract_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    contract_id: Mapped[str | None] = mapped_column(
        ForeignKey("contracts.id"), nullable=True, index=True
    )
    review_id: Mapped[str | None] = mapped_column(
        ForeignKey("engine_reviews.id"), nullable=True, index=True
    )
    # Tenant scope (PL-7) — denormalized from the parent contract; bootstrap default for contract-less
    # rows. Set explicitly on insert (PL-8).
    org_id: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DEFAULT_ORG_ID,
        server_default=DEFAULT_ORG_ID,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    event_type: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(Text, default="")

    contract: Mapped[Contract | None] = relationship(back_populates="events")
