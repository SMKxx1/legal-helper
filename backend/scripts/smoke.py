"""Smoke-test a running Legal Helper deployment.

Usage:
  python scripts/smoke.py <base-url> <username> <password>
  python scripts/smoke.py <base-url> <username> <password> --with-review <openrouter-key>

Validates the full deployment click-path (plan §6 Phase 5):
- /healthz returns 200 on 10/10 checks
- /api/status is reachable
- /api/* returns 401 without a token
- login succeeds and returns a bearer token
- /api/me returns the user
- /api/me/usage responds fast (p95 < 500ms over 20 calls)
- optional: run a quick review of a sample document (requires --with-review and an OpenRouter key)
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import httpx


def log_pass(msg: str) -> None:
    print(f"✓ {msg}")


def log_fail(msg: str) -> None:
    print(f"✗ {msg}")


def test_healthz(base_url: str, n: int = 10) -> bool:
    """Check /healthz returns 200."""
    url = base_url.rstrip("/") + "/healthz"
    results = []
    for i in range(n):
        try:
            resp = httpx.get(url, timeout=10)
            ok = resp.status_code == 200
            results.append(ok)
        except httpx.HTTPError as exc:
            log_fail(f"/healthz check {i+1}/{n}: {exc}")
            return False
    if all(results):
        log_pass(f"/healthz returns 200 on {n}/{n} checks")
        return True
    passed = sum(results)
    log_fail(f"/healthz: {passed}/{n} checks passed")
    return False


def test_status(base_url: str) -> bool:
    """Check /api/status is reachable."""
    url = base_url.rstrip("/") + "/api/status"
    try:
        resp = httpx.get(url, timeout=10)
        if resp.status_code == 200:
            log_pass("/api/status is reachable")
            return True
        log_fail(f"/api/status returned {resp.status_code}")
        return False
    except httpx.HTTPError as exc:
        log_fail(f"/api/status: {exc}")
        return False


def test_401_without_token(base_url: str) -> bool:
    """Check /api/me returns 401 without a token."""
    url = base_url.rstrip("/") + "/api/me"
    try:
        resp = httpx.get(url, timeout=10)
        if resp.status_code == 401:
            log_pass("/api/me returns 401 without token")
            return True
        log_fail(f"/api/me without token returned {resp.status_code}, expected 401")
        return False
    except httpx.HTTPError as exc:
        log_fail(f"/api/me (no token): {exc}")
        return False


def test_login(base_url: str, username: str, password: str) -> str | None:
    """Log in and return the bearer token, or None if login failed."""
    url = base_url.rstrip("/") + "/api/auth/login"
    try:
        resp = httpx.post(url, json={"username": username, "password": password}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            token = data.get("token")
            if token:
                log_pass(f"Login succeeded for {username}")
                return token
            log_fail(f"Login returned 200 but no token in response")
            return None
        log_fail(f"Login returned {resp.status_code}")
        return None
    except httpx.HTTPError as exc:
        log_fail(f"Login: {exc}")
        return None


def test_me(base_url: str, token: str) -> bool:
    """Check /api/me returns the user."""
    url = base_url.rstrip("/") + "/api/me"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = httpx.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            username = data.get("username")
            log_pass(f"/api/me returned user {username}")
            return True
        log_fail(f"/api/me returned {resp.status_code}")
        return False
    except httpx.HTTPError as exc:
        log_fail(f"/api/me: {exc}")
        return False


def test_usage_performance(base_url: str, token: str, n: int = 20) -> bool:
    """Check /api/me/usage responds fast (p95 < 500ms)."""
    url = base_url.rstrip("/") + "/api/me/usage"
    headers = {"Authorization": f"Bearer {token}"}
    latencies = []
    for i in range(n):
        try:
            start = time.time()
            resp = httpx.get(url, headers=headers, timeout=10)
            elapsed = (time.time() - start) * 1000  # ms
            if resp.status_code == 200:
                latencies.append(elapsed)
            else:
                log_fail(f"/api/me/usage call {i+1}/{n} returned {resp.status_code}")
                return False
        except httpx.HTTPError as exc:
            log_fail(f"/api/me/usage call {i+1}/{n}: {exc}")
            return False
    if latencies:
        p95 = statistics.quantiles(latencies, n=20)[18]  # 95th percentile
        if p95 < 500:
            log_pass(f"/api/me/usage p95={p95:.0f}ms over {n} calls (< 500ms)")
            return True
        log_fail(f"/api/me/usage p95={p95:.0f}ms over {n} calls (expected < 500ms)")
        return False
    return False


def test_sample_review(base_url: str, token: str, openrouter_key: str) -> bool:
    """Run a quick review of a sample document (requires --with-review and an OpenRouter key)."""
    # Find the sample .docx in the repo
    sample_path = Path(__file__).parent.parent.parent / "samples" / "nda_missing_governing_law.docx"
    if not sample_path.exists():
        log_fail(f"Sample document not found at {sample_path}")
        return False

    url = base_url.rstrip("/") + "/api/reviews"
    headers = {"Authorization": f"Bearer {token}"}

    # Upload the document
    try:
        with open(sample_path, "rb") as f:
            files = {"document": ("nda_missing_governing_law.docx", f, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
            data = {"mode": "quick"}
            resp = httpx.post(url, files=files, data=data, headers=headers, timeout=180)

        if resp.status_code == 200:
            review = resp.json()
            review_id = review.get("id")
            log_pass(f"Quick review completed (id={review_id})")
            return True
        elif resp.status_code == 202:
            review = resp.json()
            review_id = review.get("id")
            log_pass(f"Deep review queued (id={review_id}), not polling in smoke test")
            return True
        else:
            log_fail(f"Review upload returned {resp.status_code}")
            return False
    except httpx.HTTPError as exc:
        log_fail(f"Review upload: {exc}")
        return False


def run(base_url: str, username: str, password: str, with_review: bool = False, openrouter_key: str | None = None) -> bool:
    """Run all smoke tests."""
    print(f"Smoke-testing {base_url}")
    print()

    results = []

    # Test 1: /healthz x10
    results.append(test_healthz(base_url, n=10))

    # Test 2: /api/status
    results.append(test_status(base_url))

    # Test 3: 401 without token
    results.append(test_401_without_token(base_url))

    # Test 4: Login
    token = test_login(base_url, username, password)
    results.append(token is not None)

    if not token:
        log_fail("Cannot continue without a token")
        return False

    # Test 5: /api/me
    results.append(test_me(base_url, token))

    # Test 6: /api/me/usage performance
    results.append(test_usage_performance(base_url, token, n=20))

    # Test 7 (optional): Sample review
    if with_review:
        if openrouter_key:
            results.append(test_sample_review(base_url, token, openrouter_key))
        else:
            log_fail("--with-review requires an OpenRouter key")

    print()
    passed = sum(results)
    total = len(results)
    print(f"Result: {passed}/{total} checks passed")
    return all(results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smoke-test a Legal Helper deployment")
    parser.add_argument("base_url", help="Base URL (e.g., https://legal-helper.railway.app)")
    parser.add_argument("username", help="Username for login")
    parser.add_argument("password", help="Password for login")
    parser.add_argument("--with-review", action="store_true", help="Run a quick review of a sample document")
    parser.add_argument("--openrouter-key", help="OpenRouter API key for --with-review")

    args = parser.parse_args()
    success = run(
        args.base_url,
        args.username,
        args.password,
        with_review=args.with_review,
        openrouter_key=args.openrouter_key,
    )
    sys.exit(0 if success else 1)
