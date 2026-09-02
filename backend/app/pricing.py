"""Token pricing for hosted Anthropic (Claude) models.

Cost = tokens × per-million-token rate. An unknown/unpriced model returns
``None`` from ``cost_for`` (billed as $0, with a one-time warning).

Default rates are the current published Anthropic API list prices (USD per 1M
tokens, standard/global, no prompt-caching or batch discount). They are editable
from the Settings page; an edited table is stored as a single ``pricing_json``
row in ``app_settings`` and merged over these defaults by ``load_pricing``.

Sources (fetched 2026-06): https://platform.claude.com/docs/en/about-claude/pricing
  Opus 4.5–4.8  $5 in / $25 out   Sonnet 4–4.6  $3 in / $15 out
  Haiku 4.5     $1 in / $5 out    Opus 4 / 4.1  $15 in / $75 out (deprecated)
"""

from __future__ import annotations

import json
import logging
import math
import time

logger = logging.getLogger(__name__)

_PRICING_KEY = "pricing_json"

#: model id -> {"input": $/MTok, "output": $/MTok}. Keys are canonical model ids;
#: dated suffixes (e.g. ``claude-opus-4-8-20251101``) match by prefix, and a bare
#: family reference ("opus"/"sonnet"/"haiku") falls back via ``_FAMILY_CANON``.
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "claude-opus-4-5": {"input": 5.0, "output": 25.0},
    "claude-opus-4-1": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "claude-haiku-3-5": {"input": 0.8, "output": 4.0},
}

#: A bare family reference resolves to the current flagship of that family so an
#: un-suffixed/unknown model still gets a sensible rate (and honours user edits to
#: the canonical row).
_FAMILY_CANON: dict[str, str] = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}


_warned_unpriced: set[str] = set()

#: In-process cache for the no-session hot path (cost_for → rate_for → load_pricing). A deep/eval
#: review prices many LLM responses; without this each one opens a SessionLocal + app_settings
#: read. Short TTL so an admin edit (which also calls :func:`_invalidate_pricing_cache`) is picked
#: up promptly even if it bypassed :func:`save_pricing`. Replicas=1 in prod, so in-process is fine.
_PRICING_TTL_S = 30.0
_pricing_cache: tuple[float, dict[str, dict[str, float]]] | None = None


def _invalidate_pricing_cache() -> None:
    """Drop the cached merged table so the next ``load_pricing()`` re-reads from the DB."""
    global _pricing_cache
    _pricing_cache = None


def _warn_unpriced(model: str) -> None:
    """Warn ONCE per model that a hosted (priceable-looking) model has no configured rate, so its
    cost is undercounted as $0 — surfaces a missing pricing row instead of silently billing free."""
    m = (model or "").lower()
    if m and m not in _warned_unpriced:
        _warned_unpriced.add(m)
        logger.warning(
            "model %r looks priceable but has NO configured rate; cost billed as $0 "
            "(undercounted) — add its rate to the pricing table.",
            model,
        )


def _strip_namespace(model: str) -> str:
    """Drop one leading ``vendor/`` namespace: OpenRouter model ids are vendor-namespaced
    (``anthropic/claude-opus-4-8``) while the pricing rows are canonical Anthropic ids."""
    return model.split("/", 1)[1] if "/" in model else model


def _looks_priceable(model: str) -> bool:
    """True only for hosted Anthropic (Claude) models — avoids a DB hit for unknowns.

    Anthropic ids always contain "claude"; OpenRouter's namespaced form
    (``anthropic/claude-…``) is priceable too (the namespace is stripped in ``rate_for``).
    An unknown model whose name merely contains a family word like "haiku" is still
    treated as unpriced (no cost), not mispriced. A non-Claude OpenRouter id stays
    unpriced here — for those, OpenRouter's reported ``usage.cost`` is the cost source.
    """
    m = _strip_namespace((model or "").lower())
    return "claude" in m


def rate_for(
    model: str, table: dict[str, dict[str, float]] | None = None
) -> dict[str, float] | None:
    """Resolve the {input, output} $/MTok rate for ``model`` (None if unpriced)."""
    if not model:
        return None
    table = table if table is not None else load_pricing()
    m = model.strip().lower()
    if m in table:
        return table[m]
    # Longest known key that is a prefix of the model id (handles dated suffixes).
    best: str | None = None
    for key in table:
        if m.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    if best is not None:
        return table[best]
    # OpenRouter namespace (``anthropic/claude-opus-4-8``): strip the vendor prefix and
    # re-resolve — BEFORE the family fallback, else ``anthropic/claude-opus-4-1`` would
    # mis-resolve to the flagship Opus rate instead of its own (pricier) row.
    if "/" in m:
        return rate_for(_strip_namespace(m), table)
    # Family keyword fallback (e.g. a generic "claude-opus-..." we don't list).
    for fam, canon in _FAMILY_CANON.items():
        if fam in m and canon in table:
            return table[canon]
    return None


# Prompt-cache multipliers vs the base input rate (provider-standard): a cache READ
# (prefix already cached) bills ~0.10x; a cache WRITE (first call that creates the
# cache entry, Anthropic only) bills ~1.25x for the default 5-minute TTL and 2x for
# the extended 1-HOUR TTL. ``input_tokens`` passed here must EXCLUDE the cached-read
# tokens so they aren't double-billed.
_CACHE_READ_MULT = 0.10
_CACHE_WRITE_MULT = 1.25  # 5m TTL (default)
_CACHE_WRITE_MULT_1H = 2.0  # 1h TTL (extended; see config.prompt_cache_1h_deep)


def cost_for(
    model: str,
    input_tokens: int,
    output_tokens: int,
    table: dict[str, dict[str, float]] | None = None,
    *,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_write_ttl: str = "5m",
) -> float | None:
    """USD cost for a model's token usage, or None when the model isn't priced.

    Cached-prefix reads are billed at a fraction of the base input rate; passing them
    here (with ``input_tokens`` excluding them) corrects the otherwise-overstated cost
    on repeated stable prefixes (e.g. the per-clause playbook block in quick mode).
    ``cache_write_ttl`` must reflect the TTL the request actually used ("5m" | "1h") —
    a 1h write bills 2x, and under-reporting it would let real spend outrun the soft cap.
    """
    if not _looks_priceable(model):
        return None
    rate = rate_for(model, table)
    if rate is None:
        _warn_unpriced(
            model
        )  # hosted model with no rate -> bill loud, not silently free
        return None
    in_rate = float(rate["input"])
    write_mult = _CACHE_WRITE_MULT_1H if cache_write_ttl == "1h" else _CACHE_WRITE_MULT
    cost = (
        (int(input_tokens or 0) / 1_000_000.0) * in_rate
        + (int(cache_read_tokens or 0) / 1_000_000.0) * in_rate * _CACHE_READ_MULT
        + (int(cache_write_tokens or 0) / 1_000_000.0) * in_rate * write_mult
        + (int(output_tokens or 0) / 1_000_000.0) * float(rate["output"])
    )
    return round(cost, 6)


# --------------------------------------------------------------------------- #
# Stored override (editable from Settings)
# --------------------------------------------------------------------------- #
def _coerce_table(raw: object) -> dict[str, dict[str, float]]:
    """Keep only well-formed {model: {input, output}} entries with numeric rates."""
    out: dict[str, dict[str, float]] = {}
    if not isinstance(raw, dict):
        return out
    for model, rate in raw.items():
        if not isinstance(model, str) or not isinstance(rate, dict):
            continue
        raw_in, raw_out = rate.get("input"), rate.get("output")
        if raw_in is None or raw_out is None:
            continue
        try:
            inp = float(raw_in)
            outp = float(raw_out)
        except (TypeError, ValueError):
            continue
        # Reject non-finite (inf/NaN from a malformed override): NaN compares false to everything
        # and would silently poison every cost computed against this rate.
        if not (math.isfinite(inp) and math.isfinite(outp)) or inp < 0 or outp < 0:
            continue
        out[model.strip().lower()] = {"input": inp, "output": outp}
    return out


def _load_override(db) -> dict[str, dict[str, float]]:
    from .models import AppSetting

    row = db.get(AppSetting, _PRICING_KEY)
    if row is None or not row.value:
        return {}
    try:
        return _coerce_table(json.loads(row.value))
    except (ValueError, TypeError):
        logger.warning("ignoring malformed %s app_setting", _PRICING_KEY)
        return {}


def load_pricing(db=None) -> dict[str, dict[str, float]]:
    """Effective pricing = defaults overlaid with any stored user edits.

    The override is non-critical metadata, so ANY failure reading it (no DB, an unmigrated
    schema with no ``app_settings`` table, a connection error) falls back to the built-in
    defaults rather than crashing the review that is only trying to price its tokens.

    When called WITHOUT a session (``db is None`` — the hot path: every provider response is
    priced via :func:`cost_for` → :func:`rate_for` → here), the merged table is served from a
    short-lived in-process cache so a deep/eval fan-out doesn't open a DB session per LLM call.
    The cache is busted by :func:`save_pricing` / :func:`clear_pricing`; an explicit ``db``
    (admin reads/writes through their own session) always bypasses it for freshness."""
    global _pricing_cache
    if db is None:
        cached = _pricing_cache
        if cached is not None and (time.monotonic() - cached[0]) < _PRICING_TTL_S:
            return {k: dict(v) for k, v in cached[1].items()}
    override: dict[str, dict[str, float]] = {}
    try:
        if db is not None:
            override = _load_override(db)
        else:
            from .db import SessionLocal

            with SessionLocal() as s:
                override = _load_override(s)
    except Exception:  # noqa: BLE001 — pricing override must never fail a review
        logger.warning("could not load pricing override; using default pricing")
    merged = {k: dict(v) for k, v in DEFAULT_PRICING.items()}
    merged.update(override)
    if db is None:
        _pricing_cache = (time.monotonic(), {k: dict(v) for k, v in merged.items()})
    return merged


def save_pricing(table: dict, db=None) -> dict[str, dict[str, float]]:
    """Persist the pricing override (full table) and return the merged effective one."""
    _invalidate_pricing_cache()
    clean = _coerce_table(table)
    payload = json.dumps(clean)

    def _store(s) -> None:
        from .models import AppSetting, _now

        row = s.get(AppSetting, _PRICING_KEY)
        if row is None:
            s.add(AppSetting(key=_PRICING_KEY, value=payload, updated_at=_now()))
        else:
            row.value = payload
            row.updated_at = _now()
        s.commit()

    if db is not None:
        _store(db)
        return load_pricing(db)
    from .db import SessionLocal

    with SessionLocal() as s:
        _store(s)
        return load_pricing(s)


def clear_pricing(db=None) -> None:
    """Drop the stored override (revert to defaults)."""
    _invalidate_pricing_cache()
    from .models import AppSetting

    def _drop(s) -> None:
        row = s.get(AppSetting, _PRICING_KEY)
        if row is not None:
            s.delete(row)
            s.commit()

    if db is not None:
        _drop(db)
        return
    from .db import SessionLocal

    with SessionLocal() as s:
        _drop(s)
