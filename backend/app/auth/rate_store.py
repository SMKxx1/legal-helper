"""Shared request-rate store with a Redis backend + an in-process fallback (PL-6).

The per-principal sliding-window request cap (429) was process-local: each replica counted only the
requests IT served, so an N-replica deployment effectively multiplied every cap by N. This module
puts the window in a SHARED store when ``REDIS_URL`` is configured (so all replicas agree), and
transparently falls back to the original in-process limiter when Redis is absent, unconfigured, or
unreachable — a Redis outage degrades to per-replica limiting, it never fails the request.

Both engine rate seams route through one store keyed by ``principal_id``: the legacy env-key path
(``service_keys.enforce_rate_limit``) and the DB service-key path (``service_account.enforce_rate``).
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections import deque

from app.api.errors import EngineError
from app.config import settings

log = logging.getLogger("nda.ratestore")

_DEFAULT_WINDOW_S = 60.0
_NAMESPACE = "nda:rl:"


# --------------------------------------------------------------------------- #
# In-process sliding window (the fallback; also the single-replica default)
# --------------------------------------------------------------------------- #
class _SlidingWindowLimiter:
    """Per-principal request cap over a rolling ``window_s`` window. In-process + lock-guarded:
    FastAPI runs sync dependencies in a threadpool, so concurrent requests touch this from many
    threads. Shared across replicas only when fronted by :class:`RedisRateStore`."""

    def __init__(self, window_s: float = _DEFAULT_WINDOW_S) -> None:
        self._window_s = window_s
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, principal_id: str, limit: int, *, now: float | None = None) -> None:
        if limit <= 0:
            return  # disabled
        t = time.monotonic() if now is None else now
        cutoff = t - self._window_s
        with self._lock:
            dq = self._hits.get(principal_id)
            if dq is None:
                dq = deque()
                self._hits[principal_id] = dq
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= limit:
                retry = max(0.0, dq[0] + self._window_s - t)
                raise EngineError(
                    429,
                    "rate_limited",
                    "Per-key request rate cap exceeded; retry shortly.",
                    {"retry_after_s": round(retry, 1)},
                )
            # Reserve a slot only for an ADMITTED request (a rejected one must not consume quota).
            dq.append(t)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class InProcessRateStore:
    """The fallback store: a single process-global :class:`_SlidingWindowLimiter`."""

    backend = "in-process"

    def __init__(self, window_s: float = _DEFAULT_WINDOW_S) -> None:
        self._limiter = _SlidingWindowLimiter(window_s)

    def check(self, principal_id: str, limit: int, *, now: float | None = None) -> None:
        self._limiter.check(principal_id, limit, now=now)

    def reset(self) -> None:
        self._limiter.reset()


# --------------------------------------------------------------------------- #
# Redis-backed sliding window (shared across replicas)
# --------------------------------------------------------------------------- #
class RedisRateStore:
    """Sliding-window cap shared across replicas via a Redis sorted set (one zset per principal).

    Each admitted request is a member scored by its wall-clock ms; a MULTI pipeline atomically evicts
    expired members, adds the candidate, counts, and refreshes the key TTL. A request that pushes the
    count past ``limit`` is rejected and its own member is rolled back so a 429 never consumes quota.
    Wall-clock (not monotonic) time is used so replicas with skew-bounded clocks agree on the window.
    A Redis error mid-check fails OPEN — the engine must not 503 on a cache hiccup."""

    backend = "redis"

    def __init__(
        self, client, window_s: float = _DEFAULT_WINDOW_S, namespace: str = _NAMESPACE
    ) -> None:
        self._client = client
        self._window_s = window_s
        self._ns = namespace
        self._counter = (
            itertools.count()
        )  # disambiguates members added within the same ms

    def _key(self, principal_id: str) -> str:
        return self._ns + principal_id

    def check(self, principal_id: str, limit: int, *, now: float | None = None) -> None:
        if limit <= 0:
            return  # disabled
        now_ms = int((time.time() if now is None else now) * 1000)
        window_ms = int(self._window_s * 1000)
        cutoff = now_ms - window_ms
        rkey = self._key(principal_id)
        member = f"{now_ms}-{next(self._counter)}"
        try:
            pipe = self._client.pipeline()
            pipe.zremrangebyscore(rkey, 0, cutoff)
            pipe.zadd(rkey, {member: now_ms})
            pipe.zcard(rkey)
            pipe.pexpire(rkey, window_ms)
            results = pipe.execute()
        except Exception as e:  # noqa: BLE001 — degrade to ALLOW on any Redis fault
            log.warning("redis rate check failed (%s); allowing request (degraded).", e)
            return
        count = int(results[2] or 0)
        if count > limit:
            retry = 0.0
            try:
                self._client.zrem(rkey, member)  # rejected -> do not consume a slot
                oldest = self._client.zrange(rkey, 0, 0, withscores=True)
                if oldest:
                    oldest_score = float(oldest[0][1])
                    retry = max(0.0, (oldest_score + window_ms - now_ms) / 1000.0)
            except Exception as e:  # noqa: BLE001
                log.warning("redis rate rollback failed (%s).", e)
            raise EngineError(
                429,
                "rate_limited",
                "Per-key request rate cap exceeded; retry shortly.",
                {"retry_after_s": round(retry, 1)},
            )

    def reset(self) -> None:
        """Best-effort flush of this namespace (test/admin hook; never raises)."""
        try:
            keys = list(self._client.scan_iter(match=self._ns + "*"))
            if keys:
                self._client.delete(*keys)
        except Exception as e:  # noqa: BLE001
            log.warning("redis rate reset failed (%s).", e)


# --------------------------------------------------------------------------- #
# Store selection (memoized on REDIS_URL; falls back to in-process)
# --------------------------------------------------------------------------- #
_store_cache: tuple[str, InProcessRateStore | RedisRateStore] | None = (
    None  # (redis_url, store)
)
_store_lock = threading.Lock()


def _connect(url: str):
    """Open + ping a Redis client (lazy import so ``redis`` is an OPTIONAL dependency). Raises if the
    package is missing or the server is unreachable — the caller then falls back to in-process."""
    import redis  # noqa: PLC0415 — optional dependency, imported only when REDIS_URL is set

    client = redis.Redis.from_url(
        url, socket_connect_timeout=0.5, socket_timeout=0.5, decode_responses=True
    )
    client.ping()
    return client


def _build_store(url: str):
    if not url:
        return InProcessRateStore()
    try:
        client = _connect(url)
    except Exception as e:  # noqa: BLE001 — missing package / bad url / unreachable -> degrade
        log.warning(
            "REDIS_URL set but Redis is unavailable (%s); using in-process rate limiting "
            "(NOT shared across replicas).",
            e,
        )
        return InProcessRateStore()
    log.info("rate limiting backed by Redis (shared across replicas).")
    return RedisRateStore(client)


def get_store():
    """The active rate store, memoized on ``REDIS_URL`` (rebuilt when the config changes — e.g. a
    test monkeypatches ``settings.redis_url``)."""
    global _store_cache
    url = (getattr(settings, "redis_url", "") or "").strip()
    cached = _store_cache
    if cached is not None and cached[0] == url:
        return cached[1]
    with _store_lock:
        cached = _store_cache
        if cached is not None and cached[0] == url:
            return cached[1]
        store = _build_store(url)
        _store_cache = (url, store)
        return store


def enforce(principal_id: str, limit: int) -> None:
    """Raise 429 if ``principal_id`` is over ``limit`` requests in the window (no-op when limit<=0)."""
    get_store().check(principal_id, int(limit or 0))


def reset() -> None:
    """Test/admin hook: drop the active store's counters AND the selection cache (so a changed
    REDIS_URL is re-read)."""
    global _store_cache
    cached = _store_cache
    if cached is not None:
        try:
            cached[
                1
            ].reset()  # cached[1] is the active store (both store types expose reset())
        except Exception:  # noqa: BLE001
            pass
    _store_cache = None
