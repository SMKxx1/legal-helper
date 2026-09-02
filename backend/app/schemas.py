"""Pydantic DTOs and canonical enumerations — the HTTP contract.

The frontend's `src/lib/types.ts` mirrors these shapes exactly. Keep them in
sync: any field added/renamed here must be reflected there.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------- #
# Canonical enumerations (stored as strings in the DB)
# --------------------------------------------------------------------------- #
class Severity(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class ProviderName(str, Enum):
    anthropic = "anthropic"


# --------------------------------------------------------------------------- #
# CLM identity / access-control enumerations (stored as strings; see app.auth.models)
# --------------------------------------------------------------------------- #
class UserRole(str, Enum):
    admin = "admin"  # manage users + allow-list + settings + all contracts/reviews
    reviewer = "reviewer"  # run quick/deep + redline + read/write contracts
    viewer = "viewer"  # read-only dashboard


class UserStatus(str, Enum):
    active = "active"
    disabled = "disabled"  # admin-disabled; cannot sign in
    locked = "locked"  # temporary brute-force lockout (see locked_until)


class ActorType(str, Enum):
    """Who performed an audited auth/admin action."""

    user = "user"  # a signed-in UserAccount
    service = "service"  # a machine principal (X-API-Key service account)
    slack = "slack"  # a Slack-originated principal (Phase 1)
    email = "email"  # an email-originated principal (Phase 1)
    system = "system"  # automated (scheduler, bootstrap)


#: The single bootstrap organization every install starts with. org_id is carried on every
#: identity/contract row from day one so adding more orgs later is not a wide schema change.
DEFAULT_ORG_ID = "00000000000000000000000000000001"
DEFAULT_ORG_NAME = "Amperesand"


# --------------------------------------------------------------------------- #
# CLM lifecycle (Phase 0.5). The STORED state machine; 'active' and 'expiring' are DERIVED
# (computed in app.clm.derive from executed + expiration_date), never stored — so the nightly
# scan never mutates rows and there is no staleness.
# --------------------------------------------------------------------------- #
class LifecycleStatus(str, Enum):
    draft = "draft"
    under_review = "under_review"
    negotiation = "negotiation"
    ready_for_signature = "ready_for_signature"
    out_for_signature = "out_for_signature"
    executed = "executed"
    expired = "expired"
    renewed = "renewed"
    terminated = "terminated"
    void = "void"


class DerivedStatus(str, Enum):
    """The display status shown to users — the stored ``lifecycle_status`` unless an ``executed``
    contract is, by its dates, ACTIVE (today < expiration) or EXPIRING (within the notice window)."""

    active = "active"
    expiring = "expiring"


class DashboardBucket(str, Enum):
    """Kanban columns the dashboard groups contracts into (derive.bucket_for)."""

    working_on = (
        "working_on"  # draft / under_review / negotiation / ready_for_signature
    )
    out_for_signature = "out_for_signature"
    active = "active"  # executed, not yet within the notice window
    expiring = (
        "expiring"  # executed, inside the notice window (or past expiry, pre-scan)
    )
    closed = "closed"  # expired / renewed / terminated / void


class DocumentRole(str, Enum):
    incoming = "incoming"  # the counterparty's draft as received
    redlined = "redlined"  # our marked-up version (engine + human edits)
    counter = "counter"  # a counterparty counter-draft
    final = "final"  # the agreed text prepared for signature
    executed = "executed"  # the signed/executed document


class ReviewEventType(str, Enum):
    """The append-only ReviewEvent vocabulary (contract/engine stream). Stored as strings; this
    enum is the canonical list so producers and the dashboard timeline agree."""

    reviewed = "reviewed"
    cache_hit = "cache_hit"
    redline = "redline"
    lifecycle_transition = "lifecycle_transition"
    document_added = "document_added"
    document_approved = "document_approved"
    sent_to_docusign = "sent_to_docusign"
    envelope_delivered = "envelope_delivered"
    document_signed = "document_signed"
    envelope_voided = "envelope_voided"
    expiry_alert = "expiry_alert"
    renewal_needed = "renewal_needed"
    renewed = "renewed"
    expired = "expired"
    terminated = "terminated"
    notified = "notified"
    entitlement_denied = "entitlement_denied"


#: Engine entitlement action keys (a UserAccount role / AllowedAccount entitlement gates these).
#: Phase 1 builds the full allow-list; Phase 0.5 maps web roles to these for the in-process wrapper.
class EngineAction(str, Enum):
    review_quick = "review.quick"
    review_deep = "review.deep"
    redline = "redline"


# Severity ordering used for sorting (High first) and adherence weighting.
SEVERITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}
SEVERITY_WEIGHT: dict[str, float] = {"high": 5.0, "medium": 2.0, "low": 0.5}


# --------------------------------------------------------------------------- #
# Output DTOs
# --------------------------------------------------------------------------- #
class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str = ""
    filename: str
    file_format: str
    is_default: bool = False
    clause_count: int = 0
    created_at: datetime


class IssueRect(BaseModel):
    """One normalised highlight band (fractions of the display-PDF page box)."""

    page: int
    x0: float
    y0: float
    x1: float
    y1: float


class InlineEditOp(BaseModel):
    """One in-place text edit — the unified primitive for manual edits AND
    accepted AI suggestions (INLINE-EDIT-PLAN §4).

    ``start``/``end`` are char offsets into the display-PDF ``full_text`` (the same
    stream ``IssuePosition.incoming_start/end`` index). ``original_text`` is the
    integrity check baked against at write time — if the document text drifted, the
    bake refuses an in-place patch for this op and reflows instead (§9). ``op``
    distinguishes a text replacement from a true redaction (remove, no reinsert).
    """

    page: int = 1
    start: int
    end: int
    original_text: str = ""
    new_text: str = ""
    op: Literal["replace", "redact"] = "replace"
    source: Literal["manual", "ai-suggestion"] = "manual"
    issue_id: str | None = None
    rects: list[IssueRect] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Provider / health DTOs
# --------------------------------------------------------------------------- #
class ProviderInfo(BaseModel):
    active: ProviderName
    model: str
    available: bool
    detail: str = ""
    models: list[str] = Field(default_factory=list)


class HealthOut(BaseModel):
    status: str = "ok"
    provider: ProviderInfo


# --------------------------------------------------------------------------- #
# Settings DTOs (runtime configuration via the Settings page)
# --------------------------------------------------------------------------- #
class SettingsOut(BaseModel):
    ai_provider: ProviderName
    anthropic_model: str
    anthropic_key_set: bool = False
    anthropic_key_hint: str = ""  # masked, e.g. "sk-…b3f2"
    # Doc Editor: PDF → editable conversion strategy ("local").
    pdf_extract_strategy: str = "local"


class SettingsUpdate(BaseModel):
    """Partial update — only provided fields change."""

    ai_provider: ProviderName | None = None
    anthropic_api_key: str | None = None  # "" clears the stored key
    anthropic_model: str | None = None
    pdf_extract_strategy: str | None = None  # "local"


# --------------------------------------------------------------------------- #
# Anthropic pricing (editable from Settings)
# --------------------------------------------------------------------------- #
class ModelRate(BaseModel):
    """Per-million-token rate for one model (USD)."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)


class PricingOut(BaseModel):
    pricing: dict[str, ModelRate]  # effective (defaults overlaid with edits)
    defaults: dict[str, ModelRate]  # built-in published rates (for "reset")


class PricingUpdate(BaseModel):
    pricing: dict[str, ModelRate]


# JSON Schema used to constrain/validate model output (strict structured output).
CLAUSE_ANALYSIS_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
        "title": {
            "type": "string",
            "description": "Short issue label, at most 8 words.",
        },
        "rationale": {
            "type": "string",
            "description": "1-3 sentences explaining the risk to us. All analysis goes here.",
        },
        "suggested_language": {
            "type": "string",
            "description": (
                "ONLY the exact replacement clause text to paste into the contract "
                "in place of the incoming clause — final, ready-to-use wording with "
                'no commentary, instructions, options, meta-phrases ("if you want", '
                '"consider", "you could"), or surrounding quotation marks. Empty '
                "string if the clause should be deleted with no replacement."
            ),
        },
        "guidance": {
            "type": "string",
            "description": (
                "Optional negotiation notes for our lawyer (fallback positions, "
                "alternatives). All commentary belongs here, never in suggested_language."
            ),
        },
    },
    "required": ["severity", "title", "rationale"],
}


# --------------------------------------------------------------------------- #
# Service-account key DTOs (P1-4 CRUD) — /api/admin/service-keys. The raw key is
# returned ONCE on create/rotate and only its sha256 is stored.
# --------------------------------------------------------------------------- #
class ServiceKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    # The bound service principal (persisted on EngineReview.actor_user_id). Omitted -> derived
    # from name as "svc:<name-slug>". NOTE: rate + monthly-spend buckets are keyed on
    # principal_id, so two ACTIVE keys sharing one principal share those buckets (deliberate for
    # rotation overlap; give independent callers distinct principals).
    principal_id: str | None = Field(default=None, min_length=1, max_length=32)
    entitlements: list[str] = Field(min_length=1)
    rate_per_min: int | None = Field(default=None, ge=0)  # None/0 -> global default
    monthly_cost_cap_usd: float | None = Field(default=None, ge=0)  # None/0 -> global


class ServiceKeyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    entitlements: list[str] | None = None
    rate_per_min: int | None = None  # 0 clears to the global default
    monthly_cost_cap_usd: float | None = None  # 0 clears to the global default
    active: bool | None = (
        None  # false = revoke (immediate: auth reads active rows only)
    )
