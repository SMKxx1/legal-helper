"""Slack Bolt route wiring (PLAN §3.3, §3.7) — fail-closed HMAC, capability gating, ack-then-process.

Mounts the real slack-bolt FastAPI adapter on a bare app and drives it with a Starlette TestClient
(no network — the signing secret is known so we forge valid/invalid v0 signatures locally):

* capability disabled  → both routes answer a clean 503, the Bolt app is never built;
* tampered signature   → Bolt rejects with 401 (fail-closed) before any handler runs;
* url_verification      → Bolt echoes the challenge (200);
* a valid app_mention  → 200 ack, then the background listener lands a bot_inbox row (ack-then-process);
* a valid interaction  → 200 ack.
"""

from __future__ import annotations

import json
import time
import urllib.parse

import pytest
from fastapi import FastAPI
from slack_sdk.signature import SignatureVerifier
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from app.bot.channels.slack import SlackIntake, mount_slack
from app.bot.models import BotInbox
from app.config import Settings

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"


@pytest.fixture
def threaded_factory(tmp_path):
    """A sessionmaker over a file SQLite that TOLERATES cross-thread use (Bolt runs listeners in a
    background thread) — mirrors app/db.py's check_same_thread=False setting."""
    import app.auth.models  # noqa: F401 - register identity tables
    import app.models  # noqa: F401 - register core + bot tables
    from app.db import Base

    engine = create_engine(
        f"sqlite:///{tmp_path / 'slack.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _enabled_settings() -> Settings:
    return Settings(
        _env_file=None,
        slack_bot_token="xoxb-test",
        slack_signing_secret=SECRET,
        nda_bot_user_id="UBOT",
    )  # type: ignore[call-arg]


def _sign(body: str, *, secret: str = SECRET) -> dict[str, str]:
    ts = str(int(time.time()))
    sig = SignatureVerifier(secret).generate_signature(timestamp=ts, body=body)
    return {"X-Slack-Signature": sig or "", "X-Slack-Request-Timestamp": ts}


def _poll_row(factory, event_key: str, timeout: float = 5.0):
    """Wait for the inbox row to reach a TERMINAL status (done/failed), past the transient
    pending/processing states the background listener moves through."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        with factory() as s:
            row = s.query(BotInbox).filter_by(event_key=event_key).one_or_none()
            if row is not None:
                last = row
                if row.status in {"done", "failed"}:
                    return row
        time.sleep(0.05)
    return last


# ---- capability gating ---------------------------------------------------------------------------
def test_routes_return_503_when_slack_disabled() -> None:
    app = FastAPI()
    mount_slack(app, Settings(_env_file=None))  # type: ignore[call-arg]  # no slack config
    client = TestClient(app)

    for path in ("/slack/events", "/slack/interactivity"):
        resp = client.post(
            path, content="{}", headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "slack_disabled"


# ---- signature verification (fail-closed) --------------------------------------------------------
def test_tampered_signature_rejected(threaded_factory) -> None:
    app = FastAPI()
    settings = _enabled_settings()
    mount_slack(
        app,
        settings,
        intake=SlackIntake(settings, threaded_factory, dispatch=lambda e: None),
    )
    client = TestClient(app)

    body = json.dumps(
        {"type": "event_callback", "event_id": "EvX", "event": {"type": "app_mention"}}
    )
    headers = {
        "X-Slack-Signature": "v0=deadbeefdeadbeef",
        "X-Slack-Request-Timestamp": str(int(time.time())),
        "Content-Type": "application/json",
    }
    resp = client.post("/slack/events", content=body, headers=headers)
    assert resp.status_code == 401


def test_url_verification_challenge_echoed(threaded_factory) -> None:
    app = FastAPI()
    settings = _enabled_settings()
    mount_slack(
        app,
        settings,
        intake=SlackIntake(settings, threaded_factory, dispatch=lambda e: None),
    )
    client = TestClient(app)

    body = json.dumps({"type": "url_verification", "challenge": "c-abc-123"})
    headers = _sign(body)
    headers["Content-Type"] = "application/json"
    resp = client.post("/slack/events", content=body, headers=headers)
    assert resp.status_code == 200
    assert "c-abc-123" in resp.text


# ---- ack-then-process end to end -----------------------------------------------------------------
def test_valid_event_acks_then_lands_inbox_row(threaded_factory) -> None:
    app = FastAPI()
    settings = _enabled_settings()
    seen: list = []
    mount_slack(
        app,
        settings,
        intake=SlackIntake(
            settings, threaded_factory, dispatch=lambda e: seen.append(e)
        ),
    )
    client = TestClient(app)

    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "EvRoute",
            "team_id": "T1",
            "event": {
                "type": "app_mention",
                "user": "U1",
                "channel": "C1",
                "ts": "1.1",
                "text": "<@UBOT> help",
            },
        }
    )
    headers = _sign(body)
    headers["Content-Type"] = "application/json"
    resp = client.post("/slack/events", content=body, headers=headers)
    assert resp.status_code == 200  # ack returned before the listener ran

    row = _poll_row(threaded_factory, "slack:EvRoute")
    assert row is not None
    assert row.status == "done"
    assert row.channel == "slack"
    assert len(seen) == 1  # dispatch ran off the ack path


def test_valid_interaction_acks(threaded_factory) -> None:
    app = FastAPI()
    settings = _enabled_settings()
    mount_slack(
        app,
        settings,
        intake=SlackIntake(settings, threaded_factory, dispatch=lambda e: None),
    )
    client = TestClient(app)

    payload = json.dumps(
        {
            "type": "block_actions",
            "user": {"id": "U1"},
            "channel": {"id": "C1"},
            "trigger_id": "t1",
            "response_url": "https://hooks.slack/x",
            "actions": [{"action_id": "template_submit", "type": "button"}],
        }
    )
    body = "payload=" + urllib.parse.quote(payload)
    headers = _sign(body)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    resp = client.post("/slack/interactivity", content=body, headers=headers)
    assert resp.status_code == 200
