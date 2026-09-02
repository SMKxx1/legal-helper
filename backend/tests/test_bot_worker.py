"""The P2 worker jobs: the IMAP-poll and bot_inbox-sweep scheduler wrappers.

The heavy lifting (normalization, dispatch, reclaim) is tested in test_bot_email_in / test_bot_dispatch;
here we assert the thin scheduler wrappers delegate correctly, are fail-soft (a job must never raise
out of a tick), and that the IMAP job's capability gate resolves as run_worker expects.
"""

from __future__ import annotations

import app.bot.channels.email_in as email_in
import app.bot.dispatch as dispatch
from app.capabilities import EMAIL_IN, CapabilityState, build_registry
from app.config import Settings
from app.worker import scheduler


# --------------------------------------------------------------------------- #
# imap_poll wrapper
# --------------------------------------------------------------------------- #
def test_imap_poll_delegates_to_poll_once(monkeypatch):
    captured = {}

    def fake_poll_once(settings):
        captured["settings"] = settings
        return 4

    monkeypatch.setattr(email_in, "poll_once", fake_poll_once)
    settings = Settings(_env_file=None, imap_host="imap.test")
    assert scheduler.imap_poll(settings) == 4
    assert captured["settings"] is settings


def test_imap_poll_is_fail_soft_on_error(monkeypatch):
    def boom(_settings):
        raise ConnectionError("mailbox unreachable")

    monkeypatch.setattr(email_in, "poll_once", boom)
    # A transient IMAP error is swallowed so the tick never crashes; returns 0.
    assert scheduler.imap_poll(Settings(_env_file=None, imap_host="imap.test")) == 0


# --------------------------------------------------------------------------- #
# bot_inbox_sweep wrapper
# --------------------------------------------------------------------------- #
def test_bot_inbox_sweep_delegates(monkeypatch):
    calls = {}

    def fake_sweep(now=None, *, session_factory=None):
        calls["now"] = now
        calls["session_factory"] = session_factory
        return 3

    monkeypatch.setattr(dispatch, "sweep_bot_inbox", fake_sweep)
    assert scheduler.bot_inbox_sweep(now=None, session_factory="SF") == 3
    assert calls["session_factory"] == "SF"


def test_bot_inbox_sweep_is_fail_soft_on_error(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(dispatch, "sweep_bot_inbox", boom)
    assert scheduler.bot_inbox_sweep() == 0


# --------------------------------------------------------------------------- #
# Capability gate used by run_worker to decide whether to schedule the IMAP poll
# --------------------------------------------------------------------------- #
def test_email_in_capability_disabled_without_imap_config():
    settings = Settings(_env_file=None)
    assert build_registry(settings).state(EMAIL_IN) is CapabilityState.DISABLED


def test_email_in_capability_enabled_with_imap_config():
    settings = Settings(
        _env_file=None,
        imap_host="imap.test",
        imap_user="bot",
        imap_password="secret",
    )
    assert build_registry(settings).state(EMAIL_IN) is CapabilityState.ENABLED
