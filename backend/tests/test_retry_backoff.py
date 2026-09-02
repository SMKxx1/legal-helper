"""The retry ladder must actually wait before retrying.

A user hit "Review failed: rate_limited" against OpenRouter's shared upstream pool, whose 429 says
in as many words: "temporarily rate-limited upstream. Please retry shortly." The ladder retried
three times with no delay at all, so all three attempts landed inside the same rate-limit window
and failed in a few milliseconds — and three consecutive retryable failures also push the circuit
breaker toward tripping.

A rate limit is a window in time. Retrying instantly cannot succeed.
"""

from __future__ import annotations

from app.ai import gateway
from app.ai.gateway import RateLimitedError, RetryableProviderError


def test_a_provider_hint_is_honoured_exactly():
    """When the provider says how long to wait, wait that long rather than guessing."""
    exc = RateLimitedError("429", retry_after=4.0)
    assert gateway._retry_delay_s(0, exc) == 4.0
    assert gateway._retry_delay_s(2, exc) == 4.0  # the hint wins over escalation


def test_an_absurd_hint_is_capped():
    """A provider asking for ten minutes must not stall a review that long."""
    exc = RateLimitedError("429", retry_after=600.0)
    assert gateway._retry_delay_s(0, exc) <= gateway._RETRY_MAX_S


def test_backoff_grows_between_attempts():
    exc = RetryableProviderError("503")
    first = [gateway._retry_delay_s(0, exc) for _ in range(20)]
    second = [gateway._retry_delay_s(1, exc) for _ in range(20)]
    assert min(first) > 0, "a zero delay is the bug this test exists for"
    assert sum(second) / len(second) > sum(first) / len(first)


def test_backoff_is_jittered():
    """Reviewer and coverage run in parallel; identical delays would retry in lockstep."""
    exc = RetryableProviderError("503")
    delays = {gateway._retry_delay_s(1, exc) for _ in range(30)}
    assert len(delays) > 1, "delays are constant — parallel agents will collide again"


def test_every_delay_stays_within_the_cap():
    exc = RetryableProviderError("503")
    for attempt in range(6):
        assert 0 < gateway._retry_delay_s(attempt, exc) <= gateway._RETRY_MAX_S


def test_the_ladder_sleeps_between_retries(monkeypatch):
    """End to end through Gateway.run: a retryable failure must be followed by a real wait."""
    slept: list[float] = []
    monkeypatch.setattr(gateway.time, "sleep", lambda s: slept.append(s))

    class _AlwaysRateLimited:
        model_id = "z-ai/glm-5.3"
        name = "openrouter"

        def complete(self, req):
            raise RateLimitedError("429 upstream pool", retry_after=2.0)

    gw = gateway.Gateway(_AlwaysRateLimited())
    req = gateway.GatewayRequest(
        role="reviewer",
        schema={
            "type": "object",
            "required": ["x"],
            "properties": {"x": {"type": "string"}},
            "additionalProperties": False,
        },
        system="s",
        task="t",
    )
    try:
        gw.run(req, max_retries=2)
    except RateLimitedError:
        pass

    assert slept, (
        "the ladder retried without waiting — instant retries cannot clear a 429"
    )
    assert all(s == 2.0 for s in slept), f"provider hint ignored: {slept}"
    assert len(slept) == 2, f"expected a wait before each retry, got {slept}"
