"""Per-review token attribution via the request-scoped usage ledger (app.ai.usage_ledger).

Gateways are lru-cached and SHARED across concurrent reviews, and the old attribution
snapshotted the shared process-wide counters at run_review entry and took the delta at
exit — so review A's delta absorbed review B's tokens under concurrency. These tests pin
the ledger contract: a review reports exactly its OWN calls' tokens (cache hits add
zero), even when two reviews run concurrently on one shared Gateway, and the ledger
propagates into ThreadPoolExecutor fan-outs via ``ctx_copy``.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.ai.gateway import Gateway, GatewayRequest, RawResult, Usage
from app.ai.usage_ledger import ctx_copy, current_ledger, track_usage
from app.engine.review_service import run_review

#: Fixed per-call token counts the fake adapter reports on every REAL call.
_IN, _OUT = 10, 5

_DOC = (
    "Section 1. Confidentiality. The Receiving Party shall keep the Confidential Information "
    "secret and shall not disclose it to any third party without prior written consent.\n"
    "Section 2. Term. This Agreement remains in effect for three (3) years."
)
_STANDARD = (
    "Section 1. Confidentiality. The Receiving Party shall keep the Confidential Information "
    "secret.\nSection 2. Term. This Agreement remains in effect for two (2) years."
)
_PLAYBOOK = {
    "positions": [
        {
            "clause_type": "term_of_confidentiality",
            "risk_weight": 3,
            "standard_position": "Confidentiality survives for two (2) years.",
        }
    ]
}
_ROUTER_OK = {
    "rationale": "Standard mutual NDA on counterparty paper.",
    "is_nda": True,
    "perspective": "mutual",
    "our_role": "receiving",
    "paper_owner": "counterparty",
    "jurisdiction": "us",
    "counterparty_type": "company",
    "suggested_variant": "company_mutual",
    "confidence": "high",
}

_SCHEMA = {
    "type": "object",
    "required": ["x"],
    "properties": {"x": {"type": "string"}},
    "additionalProperties": False,
}


def _req(task: str = "task") -> GatewayRequest:
    return GatewayRequest(role="t", schema=_SCHEMA, system="sys", task=task)


class FakeAdapter:
    """ProviderAdapter double: canned JSON per ``req.role``, fixed per-call token usage,
    an optional per-call sleep (to force overlap in the concurrency test), and a
    thread-safe call counter."""

    name = "fake"
    model_id = "fake-model"

    def __init__(self, responses: dict[str, dict], *, sleep_s: float = 0.0) -> None:
        self.responses = responses
        self.sleep_s = sleep_s
        self.calls = 0
        self._lock = threading.Lock()

    def complete(self, req):
        with self._lock:
            self.calls += 1
        if self.sleep_s:
            time.sleep(self.sleep_s)
        obj = self.responses.get(req.role)
        assert obj is not None, f"no canned response for role {req.role!r}"
        return RawResult(
            text=json.dumps(obj),
            usage=Usage(input_tokens=_IN, output_tokens=_OUT, cost_usd=0.001),
            model_version="fake-model",
        )


def _quick_review(doc: str, gw: Gateway, gw_router: Gateway):
    """The production QUICK shape (whole-doc only + router), offline via the fakes."""
    return run_review(
        gw,
        incoming_text=doc,
        standard_text=_STANDARD,
        playbook=_PLAYBOOK,
        playbook_version="v-test",
        clause_pass=False,
        whole_doc=True,
        skip_coverage=True,
        self_verify=False,
        cross_clause=False,
        wholedoc_style="triage",
        gw_router=gw_router,
        mode_label="quick",
    )


# --------------------------------------------------------------------------- #
# (a) Single review: reported tokens == the sum of the fake adapter's per-call
# usage; a response-cache hit contributes zero.
# --------------------------------------------------------------------------- #
def test_single_review_reports_exactly_its_own_call_usage():
    primary = FakeAdapter({"wholedoc": {"findings": []}})
    router = FakeAdapter({"router": _ROUTER_OK})

    result = _quick_review(_DOC, Gateway(primary), Gateway(router))

    calls = primary.calls + router.calls
    assert calls == 2  # one wholedoc read + one router classification
    assert result.input_tokens == calls * _IN
    assert result.output_tokens == calls * _OUT


def test_response_cache_hit_adds_nothing_to_the_ledger():
    adapter = FakeAdapter({"t": {"x": "ok"}})
    gw = Gateway(adapter)

    with track_usage() as ledger:
        gw.run(_req(), max_retries=0)
        gw.run(_req(), max_retries=0)  # identical request -> response-cache hit

    assert adapter.calls == 1  # the second run never hit the provider
    assert ledger.input_tokens == _IN
    assert ledger.output_tokens == _OUT


def test_prior_tokens_are_folded_in():
    primary = FakeAdapter({"wholedoc": {"findings": []}})
    result = run_review(
        Gateway(primary),
        incoming_text=_DOC,
        standard_text=_STANDARD,
        playbook=_PLAYBOOK,
        playbook_version="v-test",
        clause_pass=False,
        whole_doc=True,
        skip_coverage=True,
        self_verify=False,
        cross_clause=False,
        wholedoc_style="triage",
        router_obj=_ROUTER_OK,  # caller pre-ran the router...
        prior_input_tokens=7,  # ...and folds its tokens in via prior_*
        prior_output_tokens=3,
        mode_label="quick",
    )
    assert result.input_tokens == _IN + 7
    assert result.output_tokens == _OUT + 3


# --------------------------------------------------------------------------- #
# (b) THE KEY TEST — no cross-contamination: two concurrent reviews on ONE
# shared Gateway each report exactly their own totals. Under the old shared-
# counter delta, each review's exit delta absorbed the other's overlapping
# calls (the per-call sleep forces the overlap), inflating both.
# --------------------------------------------------------------------------- #
def test_concurrent_reviews_on_a_shared_gateway_do_not_cross_contaminate():
    adapter = FakeAdapter(
        {"wholedoc": {"findings": []}, "router": _ROUTER_OK}, sleep_s=0.03
    )
    gw = Gateway(adapter)  # ONE gateway serving both reviews (the lru-cache shape)
    # Distinct documents so neither review is served from the response cache.
    doc_a = _DOC + "\nSection 3. Governing law of Singapore."
    doc_b = _DOC + "\nSection 3. Governing law of Delaware."

    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_a = ex.submit(_quick_review, doc_a, gw, gw)
        fut_b = ex.submit(_quick_review, doc_b, gw, gw)
        result_a, result_b = fut_a.result(), fut_b.result()

    assert adapter.calls == 4  # 2 reviews x (router + wholedoc)
    for result in (result_a, result_b):
        assert result.input_tokens == 2 * _IN  # exactly its own 2 calls, not 4
        assert result.output_tokens == 2 * _OUT


# --------------------------------------------------------------------------- #
# (c) Thread propagation: a track_usage() ledger sees usage from gateway calls
# made inside a ThreadPoolExecutor when the callable is wrapped with ctx_copy.
# --------------------------------------------------------------------------- #
def test_ledger_propagates_into_thread_pools_via_ctx_copy():
    adapter = FakeAdapter({"t": {"x": "ok"}})
    gw = Gateway(adapter)

    def call(i: int) -> None:
        gw.run(_req(task=f"task-{i}"), max_retries=0)  # distinct tasks: no cache hits

    with track_usage() as ledger, ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(ctx_copy(call), range(4)))

    assert adapter.calls == 4
    assert ledger.input_tokens == 4 * _IN
    assert ledger.output_tokens == 4 * _OUT


def test_bare_thread_pool_without_ctx_copy_loses_the_ledger():
    # Documents WHY every fan-out site must wrap with ctx_copy: executor workers
    # start with an empty context, so an unwrapped callable reports nowhere.
    adapter = FakeAdapter({"t": {"x": "ok"}})
    gw = Gateway(adapter)

    with track_usage() as ledger, ThreadPoolExecutor(max_workers=1) as ex:
        ex.submit(gw.run, _req(task="bare"), max_retries=0).result()

    assert adapter.calls == 1
    assert ledger.input_tokens == 0  # the worker thread saw no ledger


def test_nested_track_usage_inner_shadows_outer():
    adapter = FakeAdapter({"t": {"x": "ok"}})
    gw = Gateway(adapter)

    with track_usage() as outer:
        with track_usage() as inner:
            gw.run(_req(task="inner"), max_retries=0)
        gw.run(_req(task="outer"), max_retries=0)

    assert inner.input_tokens == _IN  # the inner block's call only
    assert outer.input_tokens == _IN  # only the call made AFTER the inner block
    assert current_ledger() is None  # fully reset on exit
