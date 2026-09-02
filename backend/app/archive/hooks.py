"""Post-archive fan-out registry (PLAN §3.10) — the seam the expiration agent hangs off of.

The watcher (and, potentially, the archive intent) calls :func:`on_archived` for every signed NDA it
successfully files. Interested subsystems register a callback via :func:`register_on_archived`; the
expiration extractor (PLAN §3.10: extraction triggers = archive-time + nightly sweep + manual Slack
commands) registers here so an archive-time drop is extracted without the watcher importing — or
knowing anything about — Airtable or the extraction path.

Deliberately a tiny, synchronous, in-process registry (the two agents register concurrently; keeping it
this small means neither owns the other's import graph): registration is a plain list, and the fan-out
is fail-soft — a callback that raises is logged and swallowed so one subscriber can never break another,
nor the archive that already succeeded. Callbacks run on the worker's watcher tick, so they should be
cheap or enqueue their own work; a slow extractor belongs behind the review job queue, not blocking here.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from ..telemetry import get_logger

log = get_logger("nda.archive.hooks")


@dataclass(frozen=True)
class ArchivedFile:
    """The subject handed to every ``on_archived`` subscriber — a filed, signed NDA (PLAN §3.10).

    Carries enough for the expiration extractor to run without re-fetching Drive: the ``pdf_bytes`` the
    watcher already downloaded for classification, the FINAL filed ``file_name`` (renamed or default),
    the destination ``file_id`` / ``folder_id``, the originating cache ``source_file_id`` +
    ``envelope_folder`` (the requester-DM/envelope-id seam), and the watcher's ``classification`` dict
    (issuer / recipient / nda_type / effective_date) so a subscriber can reuse it instead of re-inferring.
    """

    file_name: str
    file_id: str = ""
    folder_id: str = ""
    pdf_bytes: bytes = b""
    source_file_id: str = ""
    envelope_folder: str = ""
    classification: dict = field(default_factory=dict)


#: An archive subscriber: ``(ArchivedFile) -> None``. Must be fail-soft-friendly (its own errors are
#: caught here, but a well-behaved subscriber logs + swallows internally too).
ArchivedHook = Callable[[ArchivedFile], None]

_HOOKS: list[ArchivedHook] = []
_LOCK = threading.Lock()


def register_on_archived(hook: ArchivedHook) -> None:
    """Register ``hook`` to run for every successfully filed NDA. Idempotent per callable object (the
    same function registered twice runs once) so a re-imported module can't double-subscribe."""
    with _LOCK:
        if hook not in _HOOKS:
            _HOOKS.append(hook)


def unregister_on_archived(hook: ArchivedHook) -> None:
    """Remove a previously-registered hook (no-op if absent) — primarily for tests / teardown."""
    with _LOCK:
        try:
            _HOOKS.remove(hook)
        except ValueError:
            pass


def clear_hooks() -> None:
    """Drop every registered hook (test isolation — the registry is process-global)."""
    with _LOCK:
        _HOOKS.clear()


def registered_count() -> int:
    """How many hooks are currently registered (introspection / assertions)."""
    with _LOCK:
        return len(_HOOKS)


def on_archived(file: ArchivedFile) -> int:
    """Fan ``file`` out to every registered hook; returns the number invoked without raising.

    Fail-soft per subscriber: a hook that raises is logged and skipped so one broken subscriber can
    never break another — nor the watcher tick, whose archive has already succeeded by the time this
    runs. A snapshot of the hook list is taken under the lock so a callback that (un)registers during
    the fan-out doesn't mutate the iteration.
    """
    with _LOCK:
        hooks = list(_HOOKS)
    delivered = 0
    for hook in hooks:
        try:
            hook(file)
            delivered += 1
        except Exception as exc:  # noqa: BLE001 — one subscriber must never break another / the tick
            log.warning(
                "archive.hook.failed",
                hook=getattr(hook, "__name__", repr(hook)),
                file_name=file.file_name,
                error=repr(exc),
            )
    return delivered


__all__ = [
    "ArchivedFile",
    "ArchivedHook",
    "register_on_archived",
    "unregister_on_archived",
    "clear_hooks",
    "registered_count",
    "on_archived",
]
