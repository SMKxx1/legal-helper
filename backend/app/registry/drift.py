"""Drift management (PLAN §3.7) — retired to a no-op shim after intake moved to Tally.

The drift subsystem existed to keep the in-house NDA *forms* in sync with the token registry: when a
token was created/deleted, or a template was published with a changed token set, it flagged the
affected forms ``needs_update`` and notified their owners. With the in-house ``/f`` form service
removed (intake is the external Tally form now), there are no forms to flag — so the emit hooks are
inert. They keep their exact call signatures (the token service in :mod:`app.registry.tokens` and the
studio publish/rollback code in :mod:`app.api.routes_studio` still call them, and the tokens-UI wires a
:class:`DriftNotifier`) and return an empty :class:`DriftResult`. Kept as a thin shim rather than
deleted so those callers and the wired notifier seam stay stable; a later cleanup can drop it once no
caller references it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..telemetry import get_logger

log = get_logger("nda.registry.drift")

#: Retained for API stability (the form-settings flag the old drift flow set). No longer written.
NEEDS_UPDATE_KEY = "needs_update"


@dataclass(frozen=True)
class DriftEvent:
    """A change to the token set (PLAN §3.7). Retained for the emit hooks' result shape."""

    kind: str  # token_created | token_deleted | template_published
    token_name: str | None = None
    template_id: str | None = None
    added_tokens: tuple[str, ...] = ()
    removed_tokens: tuple[str, ...] = ()


@dataclass(frozen=True)
class DriftResult:
    """What an emit did — now always empty (no in-house forms to flag/notify)."""

    event: DriftEvent
    affected_form_ids: tuple[str, ...] = ()
    notified: int = 0


@dataclass
class DriftNotifier:
    """Retained no-op notifier seam. Intake moved to Tally, so there are no form owners to notify;
    ``notify`` is a no-op kept so the studio/tokens-UI wiring (``DriftNotifier(service=…)``) is stable."""

    service: Any | None = None

    def notify(self, **_kwargs: Any) -> bool:
        return False


def _noop(event: DriftEvent) -> DriftResult:
    log.debug(
        "registry.drift.noop",
        kind=event.kind,
        token=event.token_name,
        template_id=event.template_id,
    )
    return DriftResult(event=event)


def emit_token_created(
    _db: Session, token_name: str, *, notifier: DriftNotifier | None = None
) -> DriftResult:
    """Drift hook (no-op): a new token was created. No in-house forms to flag; kept for API stability."""
    return _noop(DriftEvent(kind="token_created", token_name=token_name))


def emit_token_deleted(
    _db: Session, token_name: str, *, notifier: DriftNotifier | None = None
) -> DriftResult:
    """Drift hook (no-op): a token was deleted. No in-house forms to flag; kept for API stability."""
    return _noop(DriftEvent(kind="token_deleted", token_name=token_name))


def emit_template_published(
    _db: Session,
    template_id: str,
    *,
    added_tokens: tuple[str, ...] | list[str] = (),
    removed_tokens: tuple[str, ...] | list[str] = (),
    notifier: DriftNotifier | None = None,
) -> DriftResult:
    """Drift hook (no-op): a template version published with a changed token set. Kept for API stability."""
    return _noop(
        DriftEvent(
            kind="template_published",
            template_id=template_id,
            added_tokens=tuple(added_tokens),
            removed_tokens=tuple(removed_tokens),
        )
    )


__all__ = [
    "NEEDS_UPDATE_KEY",
    "DriftEvent",
    "DriftResult",
    "DriftNotifier",
    "emit_token_created",
    "emit_token_deleted",
    "emit_template_published",
]
