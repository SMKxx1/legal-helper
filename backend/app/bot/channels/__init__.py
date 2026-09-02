"""Channel adapters for the in-process bot (PLAN §3.2).

Each intake channel normalizes its wire format into the canonical
:class:`~app.bot.envelope.Envelope`; each delivery channel implements the
:class:`~app.bot.channels.protocol.ReplySink` so the intent handlers post replies through one
channel-agnostic seam. This wave lands the email side (``email_in`` IMAP intake, ``email_out`` SMTP
delivery); the Slack side (``slack``) lands beside it under the same protocol.
"""

from __future__ import annotations
