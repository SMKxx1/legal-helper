"""Provider boundary — the one place that knows the Anthropic (Claude) API specifics.

Everything above this layer is provider-neutral: it hands the gateway a role, a
portable schema (``engine.portable_schema``), a stable→volatile set of prompt
blocks, and a neutral ``effort``. The gateway maps that onto the active adapter,
enforces the prompt-caching ordering, runs the retry/fallback policy, validates
the structured output, and records metrics.

Design rules realized here (the engine overview lives in ``docs/ARCHITECTURE.md``):
- one shared schema → Claude ``output_config.format`` json_schema; never
  send ``temperature``;
- stable content first so Claude's ``cache_control`` breakpoint hits the same
  prefix across calls;
- effort: neutral ``low|medium|high`` (+ ``min``/``max`` extremes) → the provider's
  native knob;
- **observable degradation** (P1-3): retryable vs terminal errors; heuristic
  fallback only at the orchestrator boundary; ``fallback_used`` metric; in
  ``eval_mode`` a failure raises instead of silently degrading.

The request *builder* and the orchestration policy are fully unit-tested with a
fake adapter; the live Anthropic ``complete()`` path wraps the SDK and is
exercised by integration tests, not unit tests (no API spend here).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import weakref
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Protocol

from app.ai.usage_ledger import current_ledger
from app.engine.portable_schema import assert_portable


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ProviderError(RuntimeError):
    """Base for provider-call failures."""


class RetryableProviderError(ProviderError):
    """Timeout, 429, 5xx, overloaded — safe to retry with backoff."""


class TerminalProviderError(ProviderError):
    """Auth, refusal, or schema-invalid after retries — do not retry."""


class SchemaValidationError(TerminalProviderError):
    """Model output did not satisfy the (portable) schema's structural contract."""


# --------------------------------------------------------------------------- #
# Neutral request / result
# --------------------------------------------------------------------------- #
EFFORTS = ("min", "low", "medium", "high", "max")

_ANTHROPIC_EFFORT = {
    "min": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "max",
}


@dataclass
class GatewayRequest:
    role: str
    schema: dict
    system: str
    task: str  # volatile — decoded last
    stable_blocks: list[str] = field(
        default_factory=list
    )  # cached prefix (playbook, doc)
    effort: str = "medium"
    max_tokens: int = 4096
    cache_key_parts: tuple = ()  # e.g. (playbook_version,) — keys the response cache
    #: Recall-safe defaults for REQUIRED fields the model omits (providers that don't hard-enforce
    #: strict json_schema — opus-4-8 via Vertex drops fields). ``{field_name: default}`` applied by
    #: leaf key inside the coercion; keeps a partial finding retained+visible rather than failing the
    #: whole review (mirrors ``_fallback_finding``). Shape-based empty defaults (array→[], nullable→
    #: null) still apply where no explicit default is given.
    coerce_defaults: dict = field(default_factory=dict)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None


@dataclass
class RawResult:
    text: str
    usage: Usage
    model_version: str


@dataclass
class Result:
    obj: dict
    usage: Usage
    model_version: str
    provider: str
    fallback_used: bool
    raw_text: str


def map_effort(provider: str, effort: str) -> str:
    if effort not in EFFORTS:
        raise ValueError(f"unknown effort {effort!r}; expected one of {EFFORTS}")
    if provider == "anthropic":
        return _ANTHROPIC_EFFORT[effort]
    raise ValueError(f"unknown provider {provider!r}")


# --------------------------------------------------------------------------- #
# Request builders (pure, testable) — the actual provider wiring
# --------------------------------------------------------------------------- #
def build_anthropic_request(
    req: GatewayRequest,
    model: str,
    *,
    include_effort: bool = True,
    cache_ttl: str = "5m",
) -> dict:
    """Claude Messages request: stable prefix in `system` with one cache breakpoint,
    task as the user turn, schema via `output_config.format`, no temperature.

    ``include_effort`` must be False for models that reject the effort param
    (Haiku 4.5, Sonnet 4.5 and older) — the adapter sets it from the model id.
    ``cache_ttl``: "5m" (the provider default — the ttl field is OMITTED so the request
    bytes are identical to before this option existed) or "1h" (explicit extended TTL;
    2x write cost — the adapter's pricing call must be told the same TTL)."""
    system_blocks: list[dict] = [{"type": "text", "text": req.system}]
    for b in req.stable_blocks:
        system_blocks.append({"type": "text", "text": b})
    cache_control: dict = {"type": "ephemeral"}  # ≤4 breakpoints; 1 here
    if cache_ttl == "1h":
        cache_control["ttl"] = "1h"
    system_blocks[-1]["cache_control"] = cache_control
    output_config: dict = {"format": {"type": "json_schema", "schema": req.schema}}
    if include_effort:
        output_config["effort"] = map_effort("anthropic", req.effort)
    return {
        "model": model,
        "max_tokens": req.max_tokens,
        "system": system_blocks,
        "messages": [{"role": "user", "content": req.task}],
        "output_config": output_config,
    }


# --------------------------------------------------------------------------- #
# Response cache + metrics
# --------------------------------------------------------------------------- #
# Whitespace-robust: also neutralizes spaced breakouts like ``< /document >`` that a lenient model
# may still read as a closing tag (strict ``</document>`` alone would miss them).
_DOC_CLOSE = re.compile(r"<\s*/\s*document\s*>", re.IGNORECASE)


def fence_document(text: str) -> str:
    """Neutralize any literal ``</document>`` in UNTRUSTED document text before it is interpolated
    into a ``<document>…</document>`` data fence — so a counterparty can't close the fence early and
    have the model treat the trailing content as instructions (prompt injection). A zero-width space
    breaks the tag while keeping the text visually identical."""
    return _DOC_CLOSE.sub("<​/document>", text or "")


def cache_key(req: GatewayRequest, model: str) -> str:
    # Hash the stable context (playbook/doc prefix) and the schema so that a
    # changed stable block or schema version invalidates the cached response.
    stable_hash = hashlib.sha256(
        "\x00".join(req.stable_blocks).encode("utf-8")
    ).hexdigest()
    schema_hash = hashlib.sha256(
        json.dumps(req.schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    # effort + max_tokens change the provider call (reasoning budget / output cap), so they MUST key
    # the cache — else a low-effort request can be served a high-effort answer cached under the same
    # role/task (or vice-versa). The deep tiebreak re-rates at effort="high"; the primary at "low".
    parts = [
        req.role,
        model,
        req.effort,
        str(req.max_tokens),
        *map(str, req.cache_key_parts),
        stable_hash,
        schema_hash,
        req.task,
    ]
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


class ResponseCache(Protocol):
    def get(self, key: str) -> Result | None: ...
    def set(self, key: str, value: Result) -> None: ...


class InMemoryResponseCache:
    def __init__(self, maxsize: int = 2048) -> None:
        # Bounded LRU + lock: one Gateway is shared across the findings threadpool.
        self._d: OrderedDict[str, Result] = OrderedDict()
        self._max = maxsize
        self._lock = threading.Lock()

    def get(self, key: str) -> Result | None:
        with self._lock:
            v = self._d.get(key)
            if v is not None:
                self._d.move_to_end(key)
            return v

    def set(self, key: str, value: Result) -> None:
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self._max:
                self._d.popitem(last=False)


class Metrics:
    """Minimal in-process recorder; replace with a real sink in deployment.

    Thread-safe + bounded: counts are O(1) and only a bounded window of recent
    events is retained (the shared Gateway is hit from many threads, indefinitely)."""

    def __init__(self, keep_recent: int = 1000) -> None:
        self._counts: dict[str, int] = {}
        self._recent: deque = deque(maxlen=keep_recent)
        # Timestamped (monotonic, name) ring for WINDOWED rates — self._counts is a lifetime
        # counter and useless for "degraded right now" health (a week-old burst would flag forever).
        self._times: deque = deque(maxlen=4096)
        self._lock = threading.Lock()
        self._clock = time.monotonic  # patchable in tests

    def incr(self, name: str, role: str, **labels: object) -> None:
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + 1
            self._recent.append((name, role, dict(labels)))
            self._times.append((self._clock(), name))

    def count(self, name: str) -> int:
        with self._lock:
            return self._counts.get(name, 0)

    def count_recent(self, name: str, window_s: float = 300.0) -> int:
        """How many ``name`` events fired within the last ``window_s`` seconds."""
        cutoff = self._clock() - window_s
        with self._lock:
            return sum(1 for ts, n in self._times if n == name and ts >= cutoff)

    @property
    def events(self) -> list[tuple[str, str, dict]]:
        with self._lock:
            return list(self._recent)


# --------------------------------------------------------------------------- #
# Adapter protocol
# --------------------------------------------------------------------------- #
class ProviderAdapter(Protocol):
    name: str
    model_id: str

    def complete(self, req: GatewayRequest) -> RawResult: ...


# --------------------------------------------------------------------------- #
# Parse / validate
# --------------------------------------------------------------------------- #
def _parse_json(text: str) -> dict:
    try:
        obj = json.loads(text)
    except (ValueError, TypeError) as e:
        raise SchemaValidationError(f"output was not valid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise SchemaValidationError("output JSON was not an object")
    return obj


def _validate_required(obj: dict, schema: dict) -> None:
    for key in schema.get("required", []):
        if key not in obj:
            raise SchemaValidationError(f"missing required field {key!r}")


# --------------------------------------------------------------------------- #
# Gateway
# --------------------------------------------------------------------------- #
#: Circuit-breaker defaults (per Gateway instance = per (mode, model), since each model gets its
#: own Gateway). Threshold counts CONSECUTIVE RetryableProviderError attempts — outage-shaped
#: failures only; a TerminalProviderError is a live provider answering and CLOSES the breaker.
#: One gateway.run() contributes up to (max_retries+1)=3 attempts, so ~2 consecutive failing runs
#: trip it. NOTE: the SDK client keeps its own internal retries (adapters.py), so each counted
#: attempt may be several HTTP attempts — the threshold is deliberately conservative.
BREAKER_THRESHOLD = 5
BREAKER_COOLDOWN_S = 30.0

#: Live gateways, for the non-gating /healthz provider field (weak: test instances vanish).
_GATEWAYS: weakref.WeakSet = weakref.WeakSet()


def provider_health(window_s: float = 300.0) -> dict:
    """Aggregate provider health across live gateways for the /healthz BODY (never the status
    code — a provider outage must not restart a container whose DB is fine). ``degraded`` when
    any breaker is open or the windowed fallback rate is elevated; per-replica state."""
    open_models: list[str] = []
    recent_fallbacks = 0
    breaker_opens = 0
    for gw in list(_GATEWAYS):
        try:
            if gw.breaker_is_open():
                open_models.append(gw.adapter.model_id)
            recent_fallbacks += gw.metrics.count_recent("fallback_used", window_s)
            breaker_opens += gw.metrics.count_recent("breaker_open", window_s)
        except Exception:  # noqa: BLE001 — health reporting must never take down /healthz
            continue
    degraded = bool(open_models) or recent_fallbacks >= 3
    return {
        "status": "degraded" if degraded else "ok",
        "breakers_open": sorted(set(open_models)),
        "recent_fallbacks": recent_fallbacks,
        "recent_breaker_opens": breaker_opens,
    }


class Gateway:
    def __init__(
        self,
        adapter: ProviderAdapter,
        cache: ResponseCache | None = None,
        metrics: Metrics | None = None,
        breaker_threshold: int = BREAKER_THRESHOLD,
        breaker_cooldown_s: float = BREAKER_COOLDOWN_S,
    ) -> None:
        self.adapter = adapter
        self.cache = cache or InMemoryResponseCache()
        self.metrics = metrics or Metrics()
        # Monotonic token usage across this gateway's real provider calls. Gateways are
        # lru-cached and SHARED across reviews, so these counters accumulate process-wide —
        # lifetime diagnostics only. Per-review attribution does NOT read them (a shared-
        # counter delta absorbs concurrent reviews' calls); it uses the request-scoped
        # ledger (app.ai.usage_ledger.track_usage), fed in run() alongside these counters.
        # Cache read/write tokens are counted separately: they are BILLED (0.1x / 1.25-2x)
        # but were previously invisible in the counters even though cost_usd included them.
        self.usage_input_tokens = 0
        self.usage_output_tokens = 0
        self.usage_cache_read_tokens = 0
        self.usage_cache_write_tokens = 0
        self._usage_lock = threading.Lock()
        # Circuit breaker (P1-3b): during a provider outage every review otherwise pays the full
        # retry ladder (up to ~3 x PROVIDER_TIMEOUT_S wall-clock) while holding a review slot.
        # Open => fail FAST down the same path an exhausted retry ladder takes (raise, or the
        # caller-supplied heuristic fallback where one exists — router/coverage degrade, the
        # whole-doc pass propagates an honest error). Per-instance = per (mode, model): an Opus
        # outage never trips the Haiku router's breaker.
        self.breaker_threshold = max(1, int(breaker_threshold))
        self.breaker_cooldown_s = float(breaker_cooldown_s)
        self._breaker_lock = threading.Lock()
        self._consec_retryable = 0
        self._open_until = 0.0
        self._probe_in_flight = False
        self._clock = time.monotonic  # patchable in tests
        _GATEWAYS.add(self)

    # -- breaker state ---------------------------------------------------- #
    def breaker_is_open(self) -> bool:
        with self._breaker_lock:
            return self._open_until > 0 and self._clock() < self._open_until

    def _breaker_admit(self) -> bool:
        """True -> proceed. False -> short-circuit (open, or a half-open probe is already out)."""
        with self._breaker_lock:
            if self._open_until <= 0:
                return True  # closed
            if self._clock() < self._open_until:
                return False  # open
            if self._probe_in_flight:
                return False  # half-open, another request is probing
            self._probe_in_flight = True  # half-open: this request is THE probe
            return True

    def _breaker_record_response(self) -> None:
        """Any completed provider RESPONSE (success or terminal error) proves reachability."""
        with self._breaker_lock:
            self._consec_retryable = 0
            self._open_until = 0.0
            self._probe_in_flight = False

    def _breaker_record_retryable(self) -> bool:
        """Count an outage-shaped failure; returns True when this one trips/re-opens the breaker."""
        with self._breaker_lock:
            self._probe_in_flight = False
            self._consec_retryable += 1
            if self._consec_retryable >= self.breaker_threshold:
                self._open_until = self._clock() + self.breaker_cooldown_s
                return True
            return False

    def run(
        self,
        req: GatewayRequest,
        *,
        code_validate: Callable[[dict], None] | None = None,
        fallback: Callable[[GatewayRequest], dict] | None = None,
        eval_mode: bool = False,
        max_retries: int = 2,
    ) -> Result:
        assert_portable(req.schema)  # never ship a non-portable schema to a provider
        key = cache_key(req, self.adapter.model_id)
        hit = self.cache.get(key)
        if hit is not None:
            self.metrics.incr("cache_hit", req.role)
            # A cache hit incurs NO new spend — its tokens were billed on the miss that populated it,
            # and the process-wide token counters below are NOT incremented on a hit. Return a
            # zero-usage copy so summed cost_usd stays consistent with the token delta and the work
            # isn't double-billed (across reviews, or on a repeated identical request within one).
            return replace(
                hit,
                usage=replace(
                    hit.usage,
                    input_tokens=0,
                    output_tokens=0,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost_usd=0.0,
                ),
            )

        # Breaker check AFTER the (free) response cache: cached answers keep serving during an
        # outage. Open => take the exhausted-retries path immediately, skipping the ladder.
        if not self._breaker_admit():
            self.metrics.incr("breaker_open", req.role, model=self.adapter.model_id)
            last_err: Exception = RetryableProviderError(
                f"provider circuit open for {self.adapter.model_id}; failing fast"
            )
            self.metrics.incr("fallback_used", req.role, reason="breaker_open")
            if eval_mode or fallback is None:
                raise last_err
            return Result(
                fallback(req),
                Usage(),
                self.adapter.model_id,
                self.adapter.name,
                True,
                "",
            )

        last: Exception | None = None
        for _attempt in range(max_retries + 1):
            try:
                raw = self.adapter.complete(req)
                self._breaker_record_response()
                obj = _parse_json(raw.text)
                _validate_required(obj, req.schema)
                if code_validate is not None:
                    code_validate(obj)  # raises TerminalProviderError on bad content
                res = Result(
                    obj,
                    raw.usage,
                    raw.model_version,
                    self.adapter.name,
                    False,
                    raw.text,
                )
                self.cache.set(key, res)
                with (
                    self._usage_lock
                ):  # real provider call → count its tokens (parallel-safe)
                    self.usage_input_tokens += raw.usage.input_tokens or 0
                    self.usage_output_tokens += raw.usage.output_tokens or 0
                    self.usage_cache_read_tokens += raw.usage.cache_read_tokens or 0
                    self.usage_cache_write_tokens += raw.usage.cache_write_tokens or 0
                # Request-scoped attribution: the caller's track_usage() ledger (if any)
                # gets this REAL call's tokens too. Cache hits return zeroed usage above
                # and never reach here; fallback results (below) add nothing either.
                ledger = current_ledger()
                if ledger is not None:
                    ledger.add(raw.usage)
                return res
            except RetryableProviderError as e:
                last = e
                if self._breaker_record_retryable():
                    # Just tripped: further attempts in THIS run are pointless too.
                    self.metrics.incr(
                        "breaker_trip", req.role, model=self.adapter.model_id
                    )
                    break
                continue
            except TerminalProviderError as e:
                self._breaker_record_response()  # the provider ANSWERED (auth/refusal/schema)
                last = e
                break
            except Exception as e:  # noqa: BLE001 — an unexpected adapter/parse defect (e.g. the
                # provider SDK returning a malformed/empty response -> a parse error) must DEGRADE via
                # the fallback like any other provider failure, never crash the whole review.
                with self._breaker_lock:
                    self._probe_in_flight = (
                        False  # a dead probe must never wedge the breaker open
                    )
                last = e
                break

        # Exhausted retries or terminal failure: observable, never silent.
        self.metrics.incr(
            "fallback_used", req.role, reason=type(last).__name__ if last else "unknown"
        )
        if eval_mode or fallback is None:
            raise last if last else TerminalProviderError("provider call failed")
        return Result(
            fallback(req), Usage(), self.adapter.model_id, self.adapter.name, True, ""
        )
