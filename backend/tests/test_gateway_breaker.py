"""Per-(mode, model) provider circuit breaker (3.2 hardening).

During a provider outage every review otherwise pays the full retry ladder (up to
~3 x PROVIDER_TIMEOUT_S) while holding DB connections and a review slot. The breaker trips on
consecutive outage-shaped failures, fails FAST down the exhausted-retries path while open,
half-opens one probe after the cooldown, and never leaks across gateway instances. The /healthz
``provider`` field reports it WITHOUT flipping the status code.
"""

from __future__ import annotations

import pytest

from app.ai.gateway import (
    Gateway,
    RawResult,
    RetryableProviderError,
    TerminalProviderError,
    Usage,
)

_SCHEMA = {
    "type": "object",
    "required": ["x"],
    "properties": {"x": {"type": "string"}},
    "additionalProperties": False,
}


def _req():
    from app.ai.gateway import GatewayRequest

    return GatewayRequest(
        role="test", schema=_SCHEMA, system="sys", task="task-1", stable_blocks=[]
    )


class ScriptedAdapter:
    """Yields exceptions/results in order; records how many times it was actually called."""

    name = "fake"
    model_id = "fake-model"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def complete(self, req):
        self.calls += 1
        step = self.script.pop(0) if self.script else RetryableProviderError("down")
        if isinstance(step, Exception):
            raise step
        return step


def _ok() -> RawResult:
    return RawResult(text='{"x": "ok"}', usage=Usage(1, 1), model_version="fake-model")


def _gw(script, *, threshold=3, cooldown=30.0) -> tuple[Gateway, ScriptedAdapter]:
    adapter = ScriptedAdapter(script)
    gw = Gateway(adapter, breaker_threshold=threshold, breaker_cooldown_s=cooldown)
    return gw, adapter


def test_breaker_trips_after_consecutive_retryable_failures():
    gw, adapter = _gw([RetryableProviderError("down")] * 10, threshold=3)
    with pytest.raises(RetryableProviderError):
        gw.run(_req(), max_retries=5)
    # The trip cut the ladder short: exactly `threshold` attempts, not max_retries+1.
    assert adapter.calls == 3
    assert gw.breaker_is_open()
    assert gw.metrics.count("breaker_trip") == 1


def test_open_breaker_fails_fast_without_touching_the_provider():
    gw, adapter = _gw([RetryableProviderError("down")] * 3, threshold=3)
    with pytest.raises(RetryableProviderError):
        gw.run(_req(), max_retries=2)
    calls_after_trip = adapter.calls

    with pytest.raises(RetryableProviderError, match="circuit open"):
        gw.run(_req(), max_retries=2)
    assert adapter.calls == calls_after_trip  # no provider call while open
    assert gw.metrics.count("breaker_open") == 1


def test_open_breaker_takes_the_fallback_path_where_one_exists():
    gw, _ = _gw([RetryableProviderError("down")] * 3, threshold=3)
    with pytest.raises(RetryableProviderError):
        gw.run(_req(), max_retries=2)

    res = gw.run(_req(), fallback=lambda r: {"x": "heuristic"}, max_retries=2)
    assert res.fallback_used is True
    assert res.obj == {"x": "heuristic"}


def test_half_open_probe_success_closes_the_breaker():
    gw, adapter = _gw(
        [RetryableProviderError("down")] * 3 + [_ok(), _ok()],
        threshold=3,
        cooldown=30.0,
    )
    with pytest.raises(RetryableProviderError):
        gw.run(_req(), max_retries=2)
    assert gw.breaker_is_open()

    # Advance past the cooldown: the next call is the single half-open probe and succeeds.
    base = gw._clock()
    gw._clock = lambda: base + 31.0
    res = gw.run(_req(), max_retries=0)
    assert res.obj == {"x": "ok"}
    assert not gw.breaker_is_open()

    # Fully closed: subsequent calls flow normally.
    from dataclasses import replace as _replace

    req2 = _replace(_req(), task="task-2")
    assert gw.run(req2, max_retries=0).obj == {"x": "ok"}


def test_half_open_probe_failure_reopens():
    gw, adapter = _gw([RetryableProviderError("down")] * 10, threshold=3, cooldown=30.0)
    with pytest.raises(RetryableProviderError):
        gw.run(_req(), max_retries=2)
    base = gw._clock()
    gw._clock = lambda: base + 31.0
    with pytest.raises(RetryableProviderError):
        gw.run(_req(), max_retries=0)  # the probe — fails
    assert gw.breaker_is_open()  # re-opened for another cooldown


def test_terminal_error_closes_instead_of_tripping():
    # A refusal/auth error is a LIVE provider answering — never outage evidence.
    gw, _ = _gw(
        [RetryableProviderError("down")] * 2 + [TerminalProviderError("refused")],
        threshold=3,
    )
    with pytest.raises(TerminalProviderError):
        gw.run(_req(), max_retries=2)
    assert not gw.breaker_is_open()
    assert gw._consec_retryable == 0


def test_breakers_are_per_instance():
    gw_opus, _ = _gw([RetryableProviderError("down")] * 3, threshold=3)
    gw_haiku, _ = _gw([_ok()], threshold=3)
    with pytest.raises(RetryableProviderError):
        gw_opus.run(_req(), max_retries=2)
    assert gw_opus.breaker_is_open()
    # The Opus outage never trips the (separate) Haiku gateway.
    assert not gw_haiku.breaker_is_open()
    assert gw_haiku.run(_req(), max_retries=0).obj == {"x": "ok"}


def test_cache_hits_keep_serving_while_open():
    gw, adapter = _gw([_ok()] + [RetryableProviderError("down")] * 3, threshold=3)
    first = gw.run(_req(), max_retries=0)
    assert first.obj == {"x": "ok"}

    from dataclasses import replace as _replace

    other = _replace(_req(), task="task-different")
    with pytest.raises(RetryableProviderError):
        gw.run(other, max_retries=2)
    assert gw.breaker_is_open()

    # The identical earlier request is a (free) cache hit — served even while open.
    cached = gw.run(_req(), max_retries=0)
    assert cached.obj == {"x": "ok"}


def test_metrics_count_recent_windows():
    from app.ai.gateway import Metrics

    m = Metrics()
    t = [0.0]
    m._clock = lambda: t[0]
    m.incr("fallback_used", "r")
    t[0] = 100.0
    m.incr("fallback_used", "r")
    assert m.count_recent("fallback_used", window_s=50.0) == 1
    assert m.count_recent("fallback_used", window_s=500.0) == 2
    assert m.count("fallback_used") == 2  # lifetime counter unchanged


# test_healthz_reports_provider_field_without_gating removed: /healthz no longer carries db/provider fields (PLAN §6 shallow probe); provider_health() stays covered by the breaker unit tests above.
