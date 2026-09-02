"""Slack thread-doc recovery (PLAN §3.9c, reference §2.8 "Slack thread-doc scan").

The envelope + archive no-file paths recover a document a user already dropped in the thread: when a
request arrives with no attachment, we scan up to 30 thread replies for a ``.docx`` / ``.doc`` / ``.pdf``
and offer it back for confirmation (reference §2.8 ``Get *Thread`` → ``Pick *Doc``).

Two pieces, split so the matching rule is exercised with zero network:

* :func:`pick_thread_doc` — the PURE port of the n8n ``Pick *Doc`` Code node: scan messages
  **newest-first**, and within a message its files **newest-first**, returning the first file whose
  name or Slack ``filetype`` is one of ``docx`` / ``doc`` / ``pdf`` (``/\\.(docx?|pdf)$/`` on the name,
  or the filetype directly). ``None`` when the thread carries no such file.
* :class:`HttpSlackThreadScanner` — the network half: ``conversations.replies`` (``limit=30``, the
  ported cap) with the bot token, then :func:`pick_thread_doc`. Network only, so the envelope handlers
  take an injectable :data:`ThreadScanner` callable and tests pass a fake.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..telemetry import get_logger

log = get_logger("nda.bot.thread_docs")

#: The ported doc-name match (reference §2.8): ``.docx`` / ``.doc`` / ``.pdf`` (case-insensitive).
_DOC_NAME_RE = re.compile(r"\.(docx?|pdf)$", re.IGNORECASE)
#: The Slack ``filetype`` values that count as a recoverable document (name-agnostic fallback).
_DOC_FILETYPES = frozenset({"docx", "doc", "pdf"})
#: The ported reply cap (reference §2.8 ``Get *Thread`` limit 30).
THREAD_REPLY_LIMIT = 30


@dataclass(frozen=True)
class ThreadDoc:
    """A document recovered from a Slack thread — a REFERENCE, never the bytes (fetched later, lazily).

    ``file_id`` is the stable Slack file id (resolved to bytes via ``files.info`` at confirm time);
    ``file_url`` is the ``url_private_download`` fallback; ``file_name`` is the display name used in the
    confirmation card and as the DocuSign document name.
    """

    file_id: str
    file_name: str
    file_url: str = ""


def _is_doc(file_obj: dict[str, Any]) -> bool:
    name = str(file_obj.get("name") or "")
    filetype = str(file_obj.get("filetype") or "").lower()
    return bool(_DOC_NAME_RE.search(name)) or filetype in _DOC_FILETYPES


def pick_thread_doc(messages: list[dict[str, Any]]) -> ThreadDoc | None:
    """The ported ``Pick *Doc`` scan (reference §2.8): newest message first, newest file first.

    Slack ``conversations.replies`` returns messages oldest-first, so newest-first is ``reversed``;
    within a message, files are scanned newest-first the same way. Returns the first ``.docx`` /
    ``.doc`` / ``.pdf`` found, or ``None``.
    """
    for msg in reversed(messages or []):
        if not isinstance(msg, dict):
            continue
        files = msg.get("files") or []
        for file_obj in reversed(files):
            if not isinstance(file_obj, dict) or not _is_doc(file_obj):
                continue
            filetype = str(file_obj.get("filetype") or "").lower()
            name = str(file_obj.get("name") or "") or f"document.{filetype or 'docx'}"
            return ThreadDoc(
                file_id=str(file_obj.get("id") or ""),
                file_name=name,
                file_url=str(
                    file_obj.get("url_private_download")
                    or file_obj.get("url_private")
                    or ""
                ),
            )
    return None


#: Recover a thread doc for ``(channel, thread_ts)`` → :class:`ThreadDoc` | ``None``. Injectable so the
#: envelope handlers run with a fake (zero network); production uses :class:`HttpSlackThreadScanner`.
ThreadScanner = Callable[[str, str], "ThreadDoc | None"]


class HttpSlackThreadScanner:
    """The default thread scanner: ``conversations.replies`` (limit 30) then :func:`pick_thread_doc`.

    Network only (httpx GET with the bot token), so tests inject a fake :data:`ThreadScanner` callable
    instead of constructing this. A missing token / API failure degrades to ``None`` (the caller then
    replies "attach the document") — recovery is best-effort, never a crash.
    """

    _API_BASE = "https://slack.com/api"

    def __init__(self, token: str, *, timeout_s: float = 30.0) -> None:
        self._token = token or ""
        self._timeout = timeout_s

    def __call__(self, channel: str, thread_ts: str) -> ThreadDoc | None:
        if not self._token or not channel or not thread_ts:
            return None
        import httpx

        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.get(
                    f"{self._API_BASE}/conversations.replies",
                    headers={"Authorization": f"Bearer {self._token}"},
                    params={
                        "channel": channel,
                        "ts": thread_ts,
                        "limit": THREAD_REPLY_LIMIT,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001 - recovery is best-effort; degrade to None
            log.warning(
                "bot.thread_docs.fetch_failed", channel=channel, error=repr(exc)
            )
            return None
        if not data.get("ok"):
            log.info("bot.thread_docs.api_not_ok", error=data.get("error", "unknown"))
            return None
        return pick_thread_doc(list(data.get("messages") or []))


__all__ = [
    "ThreadDoc",
    "ThreadScanner",
    "HttpSlackThreadScanner",
    "pick_thread_doc",
    "THREAD_REPLY_LIMIT",
]
