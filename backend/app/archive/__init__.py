"""Signed-NDA archive + the Drive cache-folder watcher (PLAN §3.10, reference §3.6/§3.11).

The archive surface has two halves that share a Google Drive account and one naming vocabulary:

* the ``archive`` intent (``app.bot.intents.archive``) — a human drops a signed NDA into Slack/email;
  the bot PDF-normalizes it and uploads it into the Drive **cache** folder (PLAN §3.10, reference §3.6);
* the **cache-folder watcher** (:mod:`app.archive.watcher`) — a worker schedule that discovers those
  drops (plus the completed envelopes that land in the cache from DocuSign), classifies each signed NDA
  (issuer / recipient / mutuality) on the cheap LLM alias, renames it to the ported
  ``<yyyyMMdd>_<issuer>_<mNDA|uNDA>_<recipient>.pdf`` convention, and files a copy into the destination
  archive folder (reference §3.11).

Both halves talk to Google Drive through ONE provider-agnostic seam,
:mod:`app.integrations.storage` (the :class:`~app.integrations.storage.base.ArchiveStorage` protocol +
:func:`~app.integrations.storage.factory.get_archive_storage`) — there is no Drive client in this
package; the destination is a config swap (Drive today, SharePoint later).

Registration note: this package is import-LIGHT on purpose. ``app.models`` imports
:mod:`app.archive.models` (see ``app/models.py``) to register ``nda_cache_processed`` on
``Base.metadata`` for the ``create_all``/Alembic-head parity gate — so this ``__init__`` must never pull
in httpx or the watcher at package load. Callers import the concrete modules
(``app.archive.watcher`` / ``app.archive.classify``) explicitly.
"""

from __future__ import annotations

__all__: list[str] = []
