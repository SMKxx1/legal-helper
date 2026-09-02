"""OpenRouter adapter — the only code that talks to OpenRouter's OpenAI-compatible API.

Implements ``gateway.ProviderAdapter.complete`` against ``POST {base_url}/chat/completions``
(httpx, no SDK dependency) for vendor-namespaced model ids (``z-ai/glm-5.3``).
This is NEW code (PLAN §3.8 — verified 2026-07-03: no prior implementation exists to port);
the direct-Anthropic adapter (``adapters.py``) remains the configuration fallback.

Contracts realized here (PLAN §3.8, §6):

- **Structured output** — the portable schema (``engine.portable_schema``) is sent as the
  OpenAI-style ``response_format: {type: "json_schema", json_schema: {name, strict, schema}}``;
  ``strict: true`` only for model families with verified structured-output support via OpenRouter.
  The schema dict is embedded AS-IS — JSON property order is the decode order under
  grammar-constrained decoding (the D1 reasoning-before-verdict rule), so nothing here may pass it
  through a key-sorting or rebuilding path. Regardless of strictness the reply is ALWAYS
  re-validated client-side against the schema; an invalid reply gets exactly ONE repair round-trip
  (same byte-stable prefix, the invalid reply + the validator error appended as new turns), after
  which ``SchemaValidationError`` (terminal) is raised.
- **Prompt structure** — mirrors ``build_anthropic_request``'s cache discipline: stable prefix
  first (system + stable blocks), volatile task decoded last, deterministic construction so the
  prefix is byte-stable across calls. For ``anthropic/*`` models the system message is a
  content-block ARRAY and the last stable block carries ``cache_control: {"type": "ephemeral"}``,
  which OpenRouter passes through to Anthropic prompt caching (≤4 breakpoints allowed; 1 used).
  ``temperature`` is never sent.
- **Effort** — the neutral effort knob maps to OpenRouter's normalized ``reasoning: {effort}``
  parameter, included ONLY for model families that accept it (the same omit-where-rejected spirit
  as ``_anthropic_supports_effort``). OpenRouter's knob is low|medium|high — the neutral "max"
  clamps to "high" (documented deviation vs the native Anthropic "max").
- **ZDR fail-closed (PLAN §6)** — under ``zdr_only`` every request carries
  ``provider: {data_collection: "deny", zdr: true, allow_fallbacks: false}`` plus optional
  ``only: [...]`` pinning. When no route satisfies these preferences OpenRouter answers with an
  error (observed as 404 "no providers available"), which maps to a TERMINAL failure here — there
  is no code path that drops the preferences to obtain a response (an error, never a downgrade).
- **Errors** — mapped onto the gateway taxonomy: 408/429/5xx/timeouts/connection failures →
  ``RetryableProviderError``; 400/401/402/403/404 (including OpenRouter's moderation 403) →
  ``TerminalProviderError``; ``finish_reason == "content_filter"`` or a native refusal →
  terminal (parity with the direct adapter's ``stop_reason == "refusal"``). A 200 whose body is
  not decodable JSON is retried once in place, then terminal.
- **Usage/cost** — every request opts into usage accounting (``usage: {include: true}`` — token
  detail + cost arrive in the same response, no second ``/generation`` lookup). OpenRouter's
  returned ``usage.cost`` (credits; 1 credit == 1 USD) is the AUTHORITATIVE ``cost_usd``
  (PLAN §3.8 — one cost source so the monthly caps don't drift); the local pricing table is only
  the fallback when no cost is reported. OpenAI-style ``prompt_tokens`` INCLUDES cached tokens,
  unlike Anthropic's ``input_tokens`` — the engine ``Usage`` uses Anthropic semantics, so cache
  read/write counts are subtracted out of ``input_tokens`` and carried separately.

BYOK note: attaching your own Anthropic key to OpenRouter is an ACCOUNT-level dashboard setting,
not a request parameter — nothing to configure per call here. For BYOK responses the true spend is
``usage.cost`` (OpenRouter's fee) plus ``usage.cost_details.upstream_inference_cost`` (the bill on
your own provider key); both are summed when present.
"""

from __future__ import annotations

import json
import re
from typing import NoReturn

import httpx

from app.ai.gateway import (
    EFFORTS,
    GatewayRequest,
    InsufficientCreditsError,
    NoZdrRouteError,
    RateLimitedError,
    RawResult,
    RetryableProviderError,
    SchemaValidationError,
    TerminalProviderError,
    Usage,
)
from app.telemetry import get_logger

log = get_logger("legal_helper.ai.openrouter")

#: Neutral effort -> OpenRouter's normalized ``reasoning.effort`` (low|medium|high). There is no
#: "max" on this knob, so both neutral extremes clamp inward — "max" runs as "high" through
#: OpenRouter (the direct-Anthropic adapter keeps native "max"; PLAN §3.8 deviation, documented).
_OPENROUTER_EFFORT = {
    "min": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "high",
}


def map_openrouter_effort(effort: str) -> str:
    if effort not in EFFORTS:
        raise ValueError(f"unknown effort {effort!r}; expected one of {EFFORTS}")
    return _OPENROUTER_EFFORT[effort]


def _is_anthropic(model: str) -> bool:
    return model.lower().startswith("anthropic/")


def _supports_reasoning(model: str) -> bool:
    """Whether to include the ``reasoning`` parameter — omit it wherever the routed model would
    reject or ignore it (the model then runs at its provider default), mirroring
    ``_anthropic_supports_effort``'s support matrix for the Anthropic family. Families are added
    here only once validated against the live API; unknown families default to omission."""
    m = model.lower()
    if _is_anthropic(m):
        # Same matrix as the direct adapter: rejected on Haiku 4.5 and Sonnet 4.5/older.
        return "haiku" not in m and "sonnet-4-5" not in m
    if m.startswith("openai/"):
        return "gpt-5" in m or m.startswith(
            "openai/o"
        )  # reasoning-capable OpenAI families
    # GLM 5.x lists `reasoning` in OpenRouter's supported_parameters; without this the agents'
    # effort settings (classifier/coverage "low", reviewer "medium") are silently dropped and
    # every tier runs at the provider default.
    return m.startswith("z-ai/glm-5")


#: Families with verified json_schema structured-output support through OpenRouter. Anything else
#: still receives the schema (strict: false) and relies on the always-on client-side validation
#: + repair round-trip.
_STRICT_FAMILIES = ("anthropic/", "openai/", "google/", "z-ai/")


def _supports_strict_schema(model: str) -> bool:
    return model.lower().startswith(_STRICT_FAMILIES)


_SCHEMA_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _schema_name(role: str) -> str:
    """``response_format.json_schema.name`` (required by the OpenAI response_format shape): a
    stable, sanitized derivative of the request role — stable so repeated calls are byte-equal."""
    return _SCHEMA_NAME_RE.sub("_", role or "output")[:64] or "output"


def _provider_prefs(zdr_only: bool, provider_only: tuple[str, ...]) -> dict:
    """OpenRouter routing preferences (PLAN §6, fail-closed). Under ZDR-only: deny provider data
    collection, admit only ZDR-qualifying endpoints, and disable fallbacks so a request can never
    silently downgrade to a non-ZDR route — an unroutable request is an ERROR. An explicit
    provider pin (``only``) also disables fallbacks: a pin that can't be honoured must not widen."""
    prefs: dict = {}
    if zdr_only:
        prefs = {"data_collection": "deny", "zdr": True, "allow_fallbacks": False}
    if provider_only:
        prefs["only"] = list(provider_only)
        prefs.setdefault("allow_fallbacks", False)
    return prefs


def build_openrouter_request(
    req: GatewayRequest,
    model: str,
    *,
    zdr_only: bool = True,
    provider_only: tuple[str, ...] = (),
    cache_ttl: str = "5m",
) -> dict:
    """Chat-completions request body: stable prefix first (system message), task as the user turn,
    schema via ``response_format``, usage accounting on, no temperature.

    For ``anthropic/*`` models the system message content is a block ARRAY whose LAST stable block
    carries the ``cache_control`` breakpoint (passed through by OpenRouter to Anthropic prompt
    caching — the same single-breakpoint discipline as ``build_anthropic_request``). Other
    families get one joined system STRING: the lowest-common-denominator OpenAI shape, with no
    caching semantics to preserve.

    ``cache_ttl``: "5m" (the provider default — the ttl field is OMITTED so request bytes are
    identical to before this option existed) or "1h" (extended TTL passed through to Anthropic;
    2x write cost — the adapter's local-pricing fallback must be told the same TTL).
    """
    system_content: str | list[dict]
    if _is_anthropic(model):
        blocks: list[dict] = [{"type": "text", "text": req.system}]
        for b in req.stable_blocks:
            blocks.append({"type": "text", "text": b})
        cache_control: dict = {"type": "ephemeral"}  # ≤4 breakpoints allowed; 1 used
        if cache_ttl == "1h":
            cache_control["ttl"] = "1h"
        blocks[-1]["cache_control"] = cache_control
        system_content = blocks
    else:
        system_content = "\n\n".join([req.system, *req.stable_blocks])
    body: dict = {
        "model": model,
        "max_tokens": req.max_tokens,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": req.task},
        ],
        # The schema dict is embedded AS-IS: property order == decode order (D1), and both dict
        # insertion order and httpx's json serialization preserve it. Never sort or rebuild it.
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": _schema_name(req.role),
                "strict": _supports_strict_schema(model),
                "schema": req.schema,
            },
        },
        # Usage accounting: token detail + OpenRouter's authoritative cost in THIS response.
        "usage": {"include": True},
    }
    if _supports_reasoning(model):
        body["reasoning"] = {"effort": map_openrouter_effort(req.effort)}
    prefs = _provider_prefs(zdr_only, provider_only)
    if prefs:
        body["provider"] = prefs
    return body


# --------------------------------------------------------------------------- #
# Client-side schema validation (always on — strict mode is not trusted alone)
# --------------------------------------------------------------------------- #
def _type_ok(value: object, t: str) -> bool:
    if t == "string":
        return isinstance(value, str)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "null":
        return value is None
    return True  # unknown type tag — portable schemas never emit one; don't over-reject


def validate_instance(obj: object, schema: dict, path: str = "$") -> str | None:
    """First structural violation of ``obj`` against the PORTABLE schema subset
    (``engine.portable_schema``): type tags (incl. nullable unions), enum membership, required
    keys, ``additionalProperties: false``, ``items``, ``anyOf``. Returns a human-readable error
    (fed verbatim to the repair turn) or None. Constraint keywords outside the portable subset are
    banned upstream by ``assert_portable``, so this validator is complete for the schemas the
    engine ships."""
    if "anyOf" in schema:
        errs = []
        for i, branch in enumerate(schema["anyOf"] or []):
            e = validate_instance(obj, branch, f"{path}|{i}")
            if e is None:
                return None
            errs.append(e)
        return f"{path}: no anyOf branch matched ({'; '.join(errs)})"
    t = schema.get("type")
    if isinstance(t, str) and not _type_ok(obj, t):
        return f"{path}: expected {t}, got {type(obj).__name__}"
    if isinstance(t, list) and not any(_type_ok(obj, x) for x in t):
        return f"{path}: expected one of {t}, got {type(obj).__name__}"
    if "enum" in schema and obj not in schema["enum"]:
        return f"{path}: value {obj!r} not in enum {schema['enum']}"
    if isinstance(obj, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in obj:
                return f"{path}: missing required field {key!r}"
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(obj) - set(props))
            if unknown:
                return f"{path}: unknown field(s) {unknown}"
        for key, sub in props.items():
            if key in obj:
                e = validate_instance(obj[key], sub, f"{path}.{key}")
                if e is not None:
                    return e
    if isinstance(obj, list) and "items" in schema:
        for i, item in enumerate(obj):
            e = validate_instance(item, schema["items"], f"{path}[{i}]")
            if e is not None:
                return e
    return None


#: Sentinel: this required field has no unambiguous empty value, so an omission is a real error.
_NO_DEFAULT = object()


def _omittable_default(sub: dict) -> object:
    """A safe default for a *required* field the model OMITTED, or ``_NO_DEFAULT``.

    Some providers/models don't hard-enforce strict ``json_schema`` (Anthropic served via
    Vertex/Bedrock through OpenRouter treats it as a strong hint, not a grammar), so a model can
    drop a field that has an unambiguous empty value: an omitted collection == ``[]``; an omitted
    nullable == ``null``. Filling those (instead of failing the whole review) is fault-isolation —
    a dropped evidence tag must not sink a completed review. Scalars (string/number/enum/bool) have
    NO safe default: those stay missing → validation fails → the existing repair round-trip runs."""
    t = sub.get("type")
    types = t if isinstance(t, list) else [t]
    if "array" in types:
        return []
    if "null" in types:
        return None
    return _NO_DEFAULT


def _coerce_omitted_defaults(
    obj: object,
    schema: dict,
    caller_defaults: dict | None = None,
    path: str = "$",
    changes: list[str] | None = None,
) -> list[str]:
    """In place, make ``obj`` conform to the strict-portable ``schema`` where a lenient provider
    diverged in a recoverable way (opus-4-8 via Vertex doesn't hard-enforce json_schema). Two
    symmetric repairs, both fault-isolation — a sloppy reply must not sink a completed review:

    * FILL a *required* key the model omitted — caller's recall-safe ``caller_defaults[key]`` (keyed
      by leaf name) first, else the schema's unambiguous empty value (see ``_omittable_default``).
    * STRIP an unknown key the model hallucinated where ``additionalProperties`` is false (the engine
      never reads it; ``additionalProperties: false`` would otherwise 400 the whole batch).

    Recurses through objects + array items. Returns the changed paths (for logging). Never overwrites
    a present, schema-valid key — including one explicitly set to ``null``."""
    if changes is None:
        changes = []
    cdef = caller_defaults or {}
    if isinstance(obj, dict) and "properties" in schema:
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in obj:
                default = (
                    cdef[key] if key in cdef else _omittable_default(props.get(key, {}))
                )
                if default is not _NO_DEFAULT:
                    obj[key] = default
                    changes.append(f"+{path}.{key}")
        if schema.get("additionalProperties") is False:
            for key in [k for k in obj if k not in props]:
                del obj[key]
                changes.append(f"-{path}.{key}")
        for key, sub in props.items():
            if key in obj:
                _coerce_omitted_defaults(obj[key], sub, cdef, f"{path}.{key}", changes)
    elif isinstance(obj, list) and "items" in schema:
        for i, item in enumerate(obj):
            _coerce_omitted_defaults(
                item, schema["items"], cdef, f"{path}[{i}]", changes
            )
    return changes


def _validate_reply(text: str, schema: dict) -> str | None:
    """None when ``text`` is a JSON object conforming to ``schema``; else the validator error."""
    try:
        obj = json.loads(text)
    except (ValueError, TypeError) as e:
        return f"reply was not valid JSON: {e}"
    if not isinstance(obj, dict):
        return "reply JSON was not an object"
    return validate_instance(obj, schema)


def _coerce_and_validate(
    text: str, schema: dict, caller_defaults: dict | None = None
) -> tuple[str, str | None]:
    """Fill omittable-default fields the model dropped, then validate. Returns ``(text, error)``:
    ``text`` is re-serialized (so the engine receives the filled fields) only when a fill happened;
    ``error`` is ``None`` on success or the first validator violation otherwise."""
    try:
        obj = json.loads(text)
    except (ValueError, TypeError) as e:
        return text, f"reply was not valid JSON: {e}"
    if not isinstance(obj, dict):
        return text, "reply JSON was not an object"
    changes = _coerce_omitted_defaults(obj, schema, caller_defaults)
    if changes:
        # '+path' = a required field filled with a default; '-path' = an unknown field stripped.
        log.info("openrouter.coerced_reply", paths=changes, count=len(changes))
        text = json.dumps(obj, ensure_ascii=False)
    return text, validate_instance(obj, schema)


def _merge_usage(a: Usage, b: Usage) -> Usage:
    """Sum two paid calls' usage (an original + its repair round-trip): both were billed."""
    costs = [c for c in (a.cost_usd, b.cost_usd) if c is not None]
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cache_write_tokens=a.cache_write_tokens + b.cache_write_tokens,
        cost_usd=round(sum(costs), 6) if costs else None,
    )


_REPAIR_INSTRUCTION = (
    "Your previous reply failed schema validation: {error}\n"
    "Reply again with ONLY a single JSON object that conforms exactly to the required "
    "schema — no prose, no markdown, no code fences."
)


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
class OpenRouterAdapter:
    name = "openrouter"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = "https://openrouter.ai/api/v1",
        zdr_only: bool = True,
        provider_only: tuple[str, ...] = (),
        timeout_s: float = 150.0,
        cache_ttl: str = "5m",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model_id = model
        self.zdr_only = bool(zdr_only)
        self.provider_only = tuple(provider_only)
        # Prompt-cache TTL for the anthropic/* stable prefix (parity with AnthropicAdapter): "5m"
        # (default — request bytes unchanged) or "1h" (2x write cost; the SAME value drives the
        # local-pricing fallback below, so an estimated cost_usd stays honest either way).
        self.cache_ttl = cache_ttl if cache_ttl == "1h" else "5m"
        # One client per adapter: gateways are lru-cached, so the connection pool persists across
        # requests. ``transport`` is an injection seam for tests (httpx.MockTransport) only.
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout_s),
            transport=transport,
        )

    # -- HTTP + error taxonomy --------------------------------------------- #
    @staticmethod
    def _raise_status(resp: httpx.Response) -> NoReturn:
        """Map a non-200 status onto the gateway taxonomy. 408/429/5xx are outage-shaped
        (retryable). Every other status answers definitively and must not be retried: 400
        validation, 401/403 auth (403 is also OpenRouter's input-moderation flag — the ``reasons``
        metadata is surfaced), 402 out of credits, and 404 = NO ROUTE SATISFIES THE ZDR/PROVIDER
        PREFERENCES — the fail-closed outcome (PLAN §6), an error rather than a downgrade."""
        try:
            raw = resp.json()
            err = raw.get("error") if isinstance(raw, dict) else None
        except ValueError:
            err = None
        if not isinstance(err, dict):
            err = {}
        msg = err.get("message") or resp.text[:200]
        detail = f"openrouter HTTP {resp.status_code}: {msg}"
        reasons = (err.get("metadata") or {}).get("reasons")
        if reasons:
            detail += f" (moderation reasons: {reasons})"
        if resp.status_code == 429:
            raise RateLimitedError(detail)
        if resp.status_code in (408,) or resp.status_code >= 500:
            raise RetryableProviderError(detail)
        if resp.status_code == 404:
            # No route satisfies zdr_only/provider_only — fail-closed (PLAN §6), never a downgrade.
            raise NoZdrRouteError(detail)
        if resp.status_code == 402:
            raise InsufficientCreditsError(detail)
        raise TerminalProviderError(detail)

    @staticmethod
    def _raise_inbody_error(data: dict) -> None:
        """OpenRouter can surface an upstream failure as an ``error`` object inside a 200 envelope
        (the provider failed after routing succeeded). Map its code exactly like an HTTP status."""
        err = data.get("error")
        if not isinstance(err, dict) or data.get("choices"):
            return
        try:
            code = int(err.get("code") or 0)
        except (TypeError, ValueError):
            code = 0
        detail = (
            f"openrouter in-body error {err.get('code')!r}: {err.get('message', '')}"
        )
        if code in (408, 429) or code >= 500:
            raise RetryableProviderError(detail)
        raise TerminalProviderError(detail)

    def _post_chat(self, body: dict) -> dict:
        """POST /chat/completions and return the decoded envelope. Transport failures are
        retryable (the gateway owns the retry ladder); a 200 whose body is not decodable JSON is
        retried ONCE in place (transient edge/proxy corruption), then terminal."""
        for attempt in (0, 1):
            try:
                resp = self._client.post("/chat/completions", json=body)
            except httpx.TimeoutException as e:
                raise RetryableProviderError(f"openrouter timeout: {e}") from e
            except httpx.TransportError as e:
                raise RetryableProviderError(f"openrouter connection error: {e}") from e
            if resp.status_code != 200:
                self._raise_status(resp)
            try:
                data = resp.json()
            except ValueError:
                if attempt == 0:
                    continue
                raise TerminalProviderError(
                    "openrouter returned an undecodable 200 body twice"
                ) from None
            if not isinstance(data, dict):
                raise TerminalProviderError("openrouter 200 body was not a JSON object")
            self._raise_inbody_error(data)
            return data
        raise AssertionError("unreachable")  # the loop always returns or raises

    # -- Response mapping ---------------------------------------------------- #
    def _extract_text(self, data: dict) -> str:
        choices = data.get("choices") or []
        if not choices:
            raise SchemaValidationError("openrouter returned no choices")
        choice = choices[0] or {}
        finish = choice.get("finish_reason")
        native = choice.get("native_finish_reason")
        # Moderation / refusal parity with the direct adapter's stop_reason == "refusal":
        # terminal, never retried (a retry just re-bills the same refusal).
        if finish == "content_filter" or native in ("refusal", "content_filter"):
            raise TerminalProviderError(
                f"openrouter content filter/refusal "
                f"(finish_reason={finish!r}, native_finish_reason={native!r})"
            )
        content = (choice.get("message") or {}).get("content")
        if isinstance(content, list):  # defensive: some providers return part arrays
            content = "".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        if not content or not isinstance(content, str):
            raise SchemaValidationError("openrouter returned no text content")
        return content

    def _map_usage(self, u: dict) -> Usage:
        """OpenRouter usage (OpenAI shape) → engine ``Usage`` (Anthropic semantics).

        OpenAI-style ``prompt_tokens`` INCLUDES cache reads (``prompt_tokens_details
        .cached_tokens``) and, on Anthropic routes, cache-creation tokens; the engine's
        ``input_tokens`` must EXCLUDE both (they bill at their own multipliers), so they are
        subtracted out. Cache-WRITE counts are not guaranteed on every route — both observed field
        placements are read, else 0 (the reported ``cost`` is still correct; only the local
        fallback estimate would under-bill writes — flagged for P1 live verification).

        ``usage.cost`` (credits == USD) is authoritative when present; for BYOK responses the
        provider's own bill arrives in ``cost_details.upstream_inference_cost`` and is added. If
        OpenRouter ever reports no cost at all, record 0 and log a warning rather than estimate one
        locally (there is no local pricing table in this engine)."""
        pt = int(u.get("prompt_tokens") or 0)
        ct = int(u.get("completion_tokens") or 0)
        ptd = u.get("prompt_tokens_details") or {}
        cache_read = int(ptd.get("cached_tokens") or 0)
        cache_write = int(
            u.get("cache_creation_input_tokens")
            or ptd.get("cache_creation_tokens")
            or 0
        )
        input_tokens = max(0, pt - cache_read - cache_write)
        cost = u.get("cost")
        upstream = (u.get("cost_details") or {}).get("upstream_inference_cost")
        cost_usd: float
        if cost is not None or upstream is not None:
            cost_usd = round(float(cost or 0.0) + float(upstream or 0.0), 6)
        else:
            cost_usd = 0.0
            log.warning("openrouter.usage_missing_cost", model=self.model_id)
        return Usage(
            input_tokens=input_tokens,
            output_tokens=ct,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cost_usd=cost_usd,
        )

    # -- ProviderAdapter ------------------------------------------------------ #
    def complete(self, req: GatewayRequest) -> RawResult:
        body = build_openrouter_request(
            req,
            self.model_id,
            zdr_only=self.zdr_only,
            provider_only=self.provider_only,
            cache_ttl=self.cache_ttl,
        )
        data = self._post_chat(body)
        text = self._extract_text(data)
        usage = self._map_usage(data.get("usage") or {})
        model_version = str(data.get("model") or self.model_id)

        # Coerce omittable-default fields the model dropped (fault-isolation for providers that don't
        # hard-enforce strict json_schema), THEN validate. ``text`` is the filled version on success.
        text, error = _coerce_and_validate(text, req.schema, req.coerce_defaults)
        if error is not None:
            # ONE repair round-trip (PLAN §3.8): the system message object is reused as-is so the
            # provider-side prompt cache still hits the same byte-stable prefix; the invalid reply
            # and the validator error are appended as new turns. A second miss is terminal.
            repair = dict(body)
            repair["messages"] = [
                *body["messages"],
                {"role": "assistant", "content": text},
                {"role": "user", "content": _REPAIR_INSTRUCTION.format(error=error)},
            ]
            data2 = self._post_chat(repair)
            text2 = self._extract_text(data2)
            usage = _merge_usage(usage, self._map_usage(data2.get("usage") or {}))
            model_version = str(data2.get("model") or model_version)
            text2, error2 = _coerce_and_validate(text2, req.schema, req.coerce_defaults)
            if error2 is not None:
                raise SchemaValidationError(
                    "openrouter output failed schema validation after one repair "
                    f"attempt: {error2}"
                )
            text = text2
        return RawResult(text=text, usage=usage, model_version=model_version)
