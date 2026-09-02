"""Smoke-test a running Legal Helper deployment.

Usage: python scripts/smoke.py <base-url> [username] [password]

Phase 0 stub: only checks that ``/healthz`` answers 200 — enough for a boot check on Railway today.
Phase 5 extends this to the full click-path check (plan §6 Phase 5): ``/healthz`` x10, ``/api/status``,
a 401 without a token, login, ``/api/me``, a timed ``/api/me/usage``, and an optional live review.
"""

from __future__ import annotations

import sys

import httpx


def run(base_url: str) -> bool:
    url = base_url.rstrip("/") + "/healthz"
    try:
        resp = httpx.get(url, timeout=10)
    except httpx.HTTPError as exc:
        print(f"FAIL  {url}  ({exc})")
        return False
    ok = resp.status_code == 200
    print(f"{'PASS' if ok else 'FAIL'}  {url}  -> {resp.status_code}")
    return ok


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python scripts/smoke.py <base-url> [username] [password]")
        sys.exit(2)
    sys.exit(0 if run(sys.argv[1]) else 1)
