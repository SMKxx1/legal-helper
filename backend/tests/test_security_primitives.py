"""Unit tests for the security-critical primitives the review panel flagged as under-covered:
the engine entitlement / minting-ceiling logic (entitlement, was 51%), and the per-principal
sliding-window rate limiter (rate_store, 41%). All pure / deterministic (the rate limiter takes
an injectable clock).

Port note (P1 wave 2.5): the source file also covered the signed machine-principal crypto
(``app.auth.principal_sig``). That whole SIGNED-principal plane is RETIRED in this engine
(``principal_sig.py`` does not exist), so the ``from app.auth import principal_sig`` import, the
six ``test_*signed*/tamper/expiry/body-binding`` tests, and their ``_SECRET``/``_signed``/``_verify``
helpers were dropped. The entitlement + rate_store coverage below is ported verbatim.
"""

from __future__ import annotations

import json

import pytest

from app.api.errors import EngineError
from app.auth.entitlement import (
    NotEntitled,
    action_for_mode,
    parse_entitlements,
    require_engine_entitlement,
)
from app.auth.rate_store import InProcessRateStore
from app.schemas import EngineAction

_RQ = EngineAction.review_quick.value  # "review.quick"
_RD = EngineAction.review_deep.value
_RL = EngineAction.redline.value


# --------------------------------------------------------------------------- #
# entitlement — the authz logic + the "minting ceiling"
# --------------------------------------------------------------------------- #
def test_parse_entitlements_clamps_to_engine_actions():
    # The minting ceiling: an allow-list / key entitlements_json can ONLY grant engine actions —
    # never a non-engine power like "admin", and junk is dropped.
    assert parse_entitlements(json.dumps([_RQ, _RL, "admin", "garbage"])) == {_RQ, _RL}
    assert parse_entitlements(None) == set()
    assert parse_entitlements("not json") == set()
    assert parse_entitlements('{"not": "a list"}') == set()


def test_action_for_mode():
    assert action_for_mode("quick") == _RQ
    assert action_for_mode("deep") == _RD
    assert action_for_mode("max") == _RD  # max ⊇ deep


class _P:  # minimal principal stand-ins
    def __init__(self, *, entitlements=None, role=None):
        if entitlements is not None:
            self.entitlements = entitlements
        if role is not None:
            self.role = role


def test_require_engine_entitlement_by_entitlements_set():
    require_engine_entitlement(_P(entitlements={_RQ}), _RQ)  # ok
    with pytest.raises(NotEntitled):
        require_engine_entitlement(_P(entitlements={_RQ}), _RL)  # not granted
    with pytest.raises(NotEntitled):
        require_engine_entitlement(_P(entitlements=set()), _RQ)  # empty -> deny


def test_require_engine_entitlement_falls_back_to_web_role():
    # A web principal exposes a role, not an entitlements set.
    require_engine_entitlement(_P(role="reviewer"), _RQ)  # ok
    with pytest.raises(NotEntitled):
        require_engine_entitlement(_P(role="viewer"), _RQ)  # viewer is read-only


# --------------------------------------------------------------------------- #
# rate_store — the sliding-window cap (deterministic via injected `now`)
# --------------------------------------------------------------------------- #
def test_sliding_window_admits_up_to_limit_then_429s():
    rs = InProcessRateStore(window_s=10)
    rs.check("p", 3, now=0)
    rs.check("p", 3, now=1)
    rs.check("p", 3, now=2)
    with pytest.raises(EngineError) as ei:
        rs.check("p", 3, now=3)  # 4th within the window
    assert ei.value.status == 429 and ei.value.code == "rate_limited"


def test_window_slides_so_old_hits_expire():
    rs = InProcessRateStore(window_s=10)
    for t in (0, 1, 2):
        rs.check("p", 3, now=t)
    # at now=11 the now=0/1 hits are outside the 10s window -> a slot frees up
    rs.check("p", 3, now=11)  # does not raise


def test_rejected_request_does_not_consume_quota_and_principals_isolated():
    rs = InProcessRateStore(window_s=10)
    rs.check("a", 1, now=0)
    with pytest.raises(EngineError):
        rs.check("a", 1, now=0)  # rejected
    with pytest.raises(EngineError):
        rs.check(
            "a", 1, now=0
        )  # still rejected (the rejected one didn't consume a slot)
    rs.check("b", 1, now=0)  # a different principal is independent -> ok


def test_limit_zero_disables_the_cap():
    rs = InProcessRateStore(window_s=10)
    for t in range(50):
        rs.check("p", 0, now=t)  # never raises when the limit is disabled
