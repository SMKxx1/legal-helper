"""Response models for the public ``/v1`` engine API.

The endpoints return ``JSONResponse`` directly (they set their own status codes — 201 new / 200 cache
hit), so attaching these as ``response_model`` is **documentation-only**: FastAPI returns the Response
unchanged at runtime but uses the model to generate the OpenAPI schema. That gives the machine callers
this API exists for (the Word add-in, n8n) a real, code-generatable contract instead of an opaque body.

``extra="allow"`` is set on the open-ended objects so a newly-added field never silently breaks the
documented shape; the named fields are the stable, relied-upon contract.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class FindingOut(BaseModel):
    """One reviewed finding (the ``_PUBLIC_FINDING_KEYS`` projection). All fields are optional because
    ``_finding_out`` emits only the keys present on a given finding (whole-doc vs clause passes differ)."""

    model_config = ConfigDict(extra="allow")

    clause_heading: str | None = None
    clause_types: list[str] | None = None
    change_type: str | None = None
    severity: str | None = None
    verified_severity: str | None = None
    title: str | None = None
    rationale: str | None = None
    suggested_language: str | None = None
    playbook_position: str | None = None
    span: str | None = None
    span_faithful: bool | None = None
    confidence: str | None = None
    guidance: str | None = None
    verify: dict | None = (
        None  # eval-only verification detail, present only when self-verify ran
    )


class RedlineEdit(BaseModel):
    """A provider-neutral edit: find a verbatim span, replace it, attach a margin comment."""

    find: str
    replace: str
    comment: str


class AbsentRequired(BaseModel):
    item_key: str | None = None
    clause_type: str | None = None
    note: str | None = None


class Coverage(BaseModel):
    absent_required: list[AbsentRequired] = []


class ReviewResponse(BaseModel):
    """The flagship ``POST /v1/reviews`` body (same shape for a fresh 201 and a cached 200)."""

    model_config = ConfigDict(extra="allow")

    review_id: str
    risk_tier: str
    adherence_score: float
    perspective: str | None = None
    playbook_version: str = ""
    routing: dict | None = None
    counts: dict | None = None
    cost_usd: float
    input_tokens: int = 0
    output_tokens: int = 0
    findings: list[FindingOut] = []
    cross_clause_flags: list = []
    coverage: Coverage
    redline_plan: list[RedlineEdit] = []


class RedlineResponse(BaseModel):
    """``POST /v1/redline`` body: the stored review's id + its provider-neutral edit plan."""

    review_id: str
    redline_plan: list[RedlineEdit] = []
