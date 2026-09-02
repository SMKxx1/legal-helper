"""The expiration-date extractor — the PLAN §3.8 benchmark contract, EXACTLY (n8n ``3epVP6vj2pPbxDdB``).

This is the reusable core of the retired "NDA Expiration Benchmark": send ONE signed NDA PDF to the
``expiration`` model alias (``google/gemini-3.5-flash`` by default) as a NATIVE-PDF file part and read
back the single agreement expiration date, strictly ``YYYY-MM-DD`` or the literal ``ERROR``. The
request shape is pinned to the benchmark's winning recipe (verified against the n8n ``Encode PDF``
node's ``geminiBody``):

    {
      "model": "<openrouter_model_expiration>",
      "max_tokens": 1000,
      "provider": {"only": ["google-vertex"], "allow_fallbacks": false,
                   "data_collection": "deny", "zdr": true},
      "reasoning": {"effort": "low", "exclude": true},
      "usage": {"include": true},
      "plugins": [{"id": "file-parser", "pdf": {"engine": "native"}}],
      "messages": [{"role": "user", "content": [
        {"type": "text", "text": <the 3-step extraction prompt>},
        {"type": "file", "file": {"filename": "document.pdf",
                                  "file_data": "data:application/pdf;base64,<b64>"}}
      ]}]
    }

Two contract details that are load-bearing and MUST NOT drift:

* **The filename is WITHHELD from the model.** The file part is always labelled the generic
  ``document.pdf`` — never the real signed-NDA name — so the model cannot cheat off jurisdiction/type
  hints encoded in filenames (the benchmark's ``SG_``/``US_`` naming convention). The real reference
  travels separately for the Airtable write, never into the request.
* **Provider pinning is ZDR fail-closed.** The provider block is built by the shared
  :func:`app.ai.openrouter._provider_prefs` (one source of truth for the ZDR routing policy —
  ``data_collection: deny``, ``zdr: true``, ``allow_fallbacks: false``) plus the ``expiration``
  alias's ``provider.only`` pin (``google-vertex`` by default; PLAN §3.8 — do NOT relax without
  re-running the eval). No code path drops the preferences to obtain a response.

This is a small DEDICATED builder, deliberately separate from ``app.ai.openrouter.OpenRouterAdapter``:
that adapter is a strict-JSON-schema, ``GatewayRequest``-shaped path for the review engine, whereas the
expiration alias sends multimodal file parts + a ``file-parser`` plugin and expects a bare-string
reply. Reusing the adapter would mean weakening it; instead we reuse only its pure provider-prefs
helper and keep this path independent (PLAN §3.8 "else a small dedicated builder — do NOT weaken the
engine adapter"). The httpx ``transport`` is an injection seam so the whole path runs on
``httpx.MockTransport`` with zero network in tests.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass

import httpx

from ..ai.openrouter import _provider_prefs
from ..telemetry import get_logger

log = get_logger("nda.expiration.extractor")

# --------------------------------------------------------------------------- #
# Pinned benchmark constants (n8n Encode PDF geminiBody) — do NOT drift
# --------------------------------------------------------------------------- #
#: The verbatim 3-step extraction prompt (n8n ``Config`` node / ``prompt_gist``). The trap it guards
#: against is Step 3 (never use the confidentiality SURVIVAL period as the agreement term).
EXPIRATION_PROMPT = (
    "You are given ONE Non-Disclosure Agreement as a PDF (it may be a scanned image; "
    "read it visually). Output the AGREEMENT'S EXPIRATION DATE. "
    "Step 1 - Effective Date: either (a) an explicit date in the opening paragraph, or "
    "(b) the date of the last signature -> if (b), read the signature block, find BOTH "
    "parties' signature dates, use the LATER of the two. "
    "Step 2 - Term: in the Term/Termination clause, duration is either (a) a length of "
    "time (e.g. two (2) years, eighteen (18) months, 90 days, 30 months, or on the Nth "
    "anniversary of the Effective Date) -> ADD to Effective Date; or (b) an explicit "
    "expiry date (e.g. shall expire on 31 December 2027) -> use directly. "
    "Step 3 - IGNORE the confidentiality survival period (obligations that survive for N "
    "years); that is NOT the agreement term. "
    "Dates appear in mixed regional formats (15/03/2025, March 15 2025, 15th March 2025, "
    "2025-03-15); infer day/month order from the document region (Singapore=day-first, "
    "US=month-first). "
    "OUTPUT RULE: respond with ONLY the expiration date as YYYY-MM-DD and nothing else; "
    "if truly indeterminable respond exactly ERROR."
)

#: The output cap the benchmark pinned (a bare date needs almost nothing; 1000 leaves reasoning room).
EXPIRATION_MAX_TOKENS = 1000
#: The benchmark's per-call HTTP timeout (120000 ms). A large scanned PDF via native vision is slow.
EXPIRATION_TIMEOUT_S = 120.0
#: The GENERIC file-part name — the real signed-NDA filename is deliberately never sent (anti-cheat).
FILE_PART_NAME = "document.pdf"
#: The strict output contract: a single ISO date, else the literal ``ERROR``.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: The reasoning knob the benchmark used: low effort, excluded from the reply (we only want the date).
_REASONING = {"effort": "low", "exclude": True}
#: The file-parser plugin: ``native`` = the model reads the PDF visually (incl. scanned images), with
#: NO OCR / text-pre-extraction step. This is the whole point of the benchmark's approach.
_PLUGINS = [{"id": "file-parser", "pdf": {"engine": "native"}}]


def is_iso_date(text: str) -> bool:
    """True iff ``text`` is exactly a ``YYYY-MM-DD`` string (the benchmark's ``/^\\d{4}-\\d{2}-\\d{2}$/``)."""
    return bool(_ISO_DATE_RE.match(text or ""))


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ExpirationError(RuntimeError):
    """Base for any expiration-extraction failure."""


class ExpirationUnavailable(ExpirationError):
    """The LLM inference capability is disabled (no OpenRouter key) — extraction is politely off.

    A CAPABILITY-off condition, not a per-document failure: callers (the archive hook, the sweep, the
    manual re-extract command) catch this and degrade, mirroring the fail-soft rule (PLAN §6).
    """


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExpirationResult:
    """The outcome of one extraction call.

    ``date`` is the ONLY thing callers write to Airtable — set to a validated ``YYYY-MM-DD`` string
    when the model returned a valid ISO date, else ``None`` (the model said ``ERROR``, returned an
    unexpected shape, or the call failed). ``status`` distinguishes those cases for ops:

    * ``"ok"``            — the model answered with a valid ISO date (``date`` set) OR the literal
      ``ERROR`` (``date`` None — a legitimate "indeterminable" verdict, exactly the benchmark's
      ``predicted='ERROR'``);
    * ``"error_output"``  — the model answered, but with neither a valid ISO date nor ``ERROR``
      (``date`` None — an off-contract reply worth flagging);
    * ``"call_failed"``   — the HTTP call itself failed (timeout, connection error, non-200, or an
      in-body upstream error). ``date`` None. Fail-soft: mirrors the benchmark's ``neverError`` — one
      bad PDF call never aborts a batch; that document simply gets no date this pass.
    """

    date: str | None
    raw: str
    status: str
    detail: str = ""
    usage: dict | None = None
    model: str = ""


# --------------------------------------------------------------------------- #
# Pure request builder (no network — golden-testable)
# --------------------------------------------------------------------------- #
def build_expiration_request(
    pdf_bytes: bytes,
    *,
    model: str,
    provider_only: tuple[str, ...] = (),
    zdr_only: bool = True,
    prompt: str = EXPIRATION_PROMPT,
    max_tokens: int = EXPIRATION_MAX_TOKENS,
    file_name: str = FILE_PART_NAME,
) -> dict:
    """Build the exact OpenRouter chat-completions body for one PDF (the benchmark ``geminiBody``).

    ``pdf_bytes`` is base64-encoded into a ``data:application/pdf;base64,…`` file part whose filename
    is ALWAYS ``file_name`` (``document.pdf`` — the real name is withheld). The provider block reuses
    :func:`app.ai.openrouter._provider_prefs` so the ZDR fail-closed policy has a single source of
    truth; ``provider_only`` carries the ``expiration`` alias's ``google-vertex`` pin.
    """
    b64 = base64.b64encode(pdf_bytes).decode("ascii")
    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "reasoning": dict(_REASONING),
        "usage": {"include": True},
        "plugins": [dict(p) for p in _PLUGINS],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "file",
                        "file": {
                            "filename": file_name,
                            "file_data": f"data:application/pdf;base64,{b64}",
                        },
                    },
                ],
            }
        ],
    }
    prefs = _provider_prefs(zdr_only, tuple(provider_only))
    if prefs:
        body["provider"] = prefs
    return body


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #
def _extract_usage(usage: dict) -> dict:
    """The benchmark's per-call usage fields → a flat dict for logging (not persisted to Airtable)."""
    ptd = usage.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "reasoning_tokens": int(ptd.get("reasoning_tokens") or 0),
        "cost_usd": usage.get("cost"),
    }


def _content_text(data: dict) -> str:
    """The model's reply text: ``choices[0].message.content`` (string, or a joined text-part array)."""
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # some providers return a content-part array
        content = "".join(
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return content if isinstance(content, str) else ""


def _classify_reply(raw: str, usage: dict, model: str) -> ExpirationResult:
    """Apply the strict output contract to a trimmed reply → a typed :class:`ExpirationResult`."""
    if is_iso_date(raw):
        return ExpirationResult(
            date=raw, raw=raw, status="ok", usage=usage, model=model
        )
    if raw == "ERROR":
        # A legitimate "indeterminable" verdict — the benchmark's ``predicted='ERROR'``. Not a failure.
        return ExpirationResult(
            date=None,
            raw=raw,
            status="ok",
            detail="model returned ERROR",
            usage=usage,
            model=model,
        )
    # Answered, but off-contract (not ISO, not ERROR). No date written; flag it for ops.
    return ExpirationResult(
        date=None,
        raw=raw,
        status="error_output",
        detail="reply was neither a YYYY-MM-DD date nor the literal ERROR",
        usage=usage,
        model=model,
    )


# --------------------------------------------------------------------------- #
# The extractor (capability-gated; fail-soft per call)
# --------------------------------------------------------------------------- #
def extract_expiration(
    pdf_bytes: bytes,
    *,
    settings=None,
    registry=None,
    transport: httpx.BaseTransport | None = None,
    timeout_s: float = EXPIRATION_TIMEOUT_S,
) -> ExpirationResult:
    """Extract the expiration date from one signed-NDA PDF via the ``expiration`` alias.

    Capability gate (PLAN §6): requires the LLM inference capability (an OpenRouter key). When
    ``registry`` is supplied its ``llm_inference`` state is authoritative; otherwise the key presence
    on ``settings`` is checked directly. A disabled capability raises :class:`ExpirationUnavailable`
    (the caller degrades) — it is NOT a per-document failure.

    Everything else is FAIL-SOFT and returned as a typed :class:`ExpirationResult` (never raised): a
    timeout / connection error / non-200 / in-body upstream error becomes ``status='call_failed'`` with
    ``date=None`` (the benchmark's ``neverError`` posture — one bad PDF never aborts a sweep).
    """
    from ..config import get_settings

    settings = settings or get_settings()
    _require_llm(settings, registry)

    model = settings.openrouter_model_expiration
    body = build_expiration_request(
        pdf_bytes,
        model=model,
        provider_only=settings.expiration_provider_only_list,
        zdr_only=settings.openrouter_zdr_only,
    )

    client = httpx.Client(
        base_url=settings.openrouter_base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
        timeout=httpx.Timeout(timeout_s),
        transport=transport,
    )
    try:
        data = _post(client, body, model)
    except _CallFailed as exc:
        log.warning("expiration.call_failed", model=model, detail=str(exc))
        return ExpirationResult(
            date=None, raw="", status="call_failed", detail=str(exc), model=model
        )
    finally:
        client.close()

    usage = _extract_usage(data.get("usage") or {})
    raw = _content_text(data).strip()
    result = _classify_reply(raw, usage, str(data.get("model") or model))
    log.info(
        "expiration.extracted",
        model=result.model,
        status=result.status,
        has_date=result.date is not None,
        cost_usd=usage.get("cost_usd"),
    )
    return result


class _CallFailed(Exception):
    """Internal: an HTTP-layer failure mapped to ``status='call_failed'`` (kept fail-soft)."""


def _post(client: httpx.Client, body: dict, model: str) -> dict:
    """POST /chat/completions once; raise :class:`_CallFailed` on any transport/status/in-body error.

    Deliberately no retry ladder: the sweep re-runs nightly and the archive hook re-drives on the next
    signal, so a transient blip just leaves the date unwritten this pass (benchmark ``neverError``).
    """
    try:
        resp = client.post("/chat/completions", json=body)
    except httpx.TimeoutException as e:
        raise _CallFailed(f"timeout: {e}") from e
    except httpx.TransportError as e:
        raise _CallFailed(f"connection error: {e}") from e
    if resp.status_code != 200:
        detail = resp.text[:200]
        try:
            err = (resp.json() or {}).get("error")
            if isinstance(err, dict) and err.get("message"):
                detail = str(err["message"])
        except ValueError:
            pass
        raise _CallFailed(f"HTTP {resp.status_code}: {detail}")
    try:
        data = resp.json()
    except ValueError as e:
        raise _CallFailed(f"undecodable 200 body: {e}") from e
    if not isinstance(data, dict):
        raise _CallFailed("200 body was not a JSON object")
    # OpenRouter can surface an upstream failure inside a 200 envelope (error object, no choices).
    err = data.get("error")
    if isinstance(err, dict) and not data.get("choices"):
        raise _CallFailed(
            f"in-body error {err.get('code')!r}: {err.get('message', '')}"
        )
    return data


def _require_llm(settings, registry) -> None:
    """Raise :class:`ExpirationUnavailable` when LLM inference is not available (capability off)."""
    if registry is not None:
        from ..capabilities import LLM_INFERENCE, CapabilityState

        if registry.state(LLM_INFERENCE) is not CapabilityState.ENABLED:
            status = registry.get(LLM_INFERENCE)
            raise ExpirationUnavailable(
                f"llm_inference capability is {status.state.value}: {status.reason}"
            )
        return
    if not (settings.openrouter_api_key or "").strip():
        raise ExpirationUnavailable(
            "llm_inference is disabled: OPENROUTER_API_KEY is not set"
        )


__all__ = [
    "EXPIRATION_PROMPT",
    "EXPIRATION_MAX_TOKENS",
    "EXPIRATION_TIMEOUT_S",
    "FILE_PART_NAME",
    "ExpirationError",
    "ExpirationUnavailable",
    "ExpirationResult",
    "build_expiration_request",
    "extract_expiration",
    "is_iso_date",
]
