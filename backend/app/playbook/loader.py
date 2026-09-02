"""The playbook: one small JSON file of standard positions the reviewer/coverage agents are
graded against (plan §4.2, §6.3). Students edit ``playbook/legal_helper_playbook.json`` directly —
this module only loads, validates, and renders it.

Shape (see the JSON file itself for the real content)::

    {"version": "lh-1", "positions": [
        {"clause_type": str, "presence": "required"|"expected"|"optional",
         "risk_weight": 1|2|3, "standard_position": str, "walk_away": str},
        ...
    ]}

``load_playbook`` fails LOUDLY (``PlaybookValidationError``) on a structurally broken file — a typo
here would otherwise silently drop a clause from the coverage checklist with no error anywhere.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

#: repo root / playbook/legal_helper_playbook.json (backend/app/playbook -> app -> backend -> repo)
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PLAYBOOK_PATH = _REPO_ROOT / "playbook" / "legal_helper_playbook.json"

_ALLOWED_PRESENCE: frozenset[str] = frozenset({"required", "expected", "optional"})
_MIN_WEIGHT, _MAX_WEIGHT = 1, 3


class PlaybookValidationError(ValueError):
    """The playbook JSON is structurally invalid. Raised at load time so a typo can't silently
    drop a clause from the checklist or the reviewer's grounding."""


@dataclass(frozen=True)
class Position:
    clause_type: str
    presence: str  # "required" | "expected" | "optional"
    risk_weight: int  # 1-3
    standard_position: str
    walk_away: str


def validate(pb: dict) -> list[Position]:
    """Structurally validate ``pb`` (the parsed JSON) and return its positions as typed
    :class:`Position` objects. Raises :class:`PlaybookValidationError` on any violation:
    unique clause types, an allowed ``presence`` value, and a ``risk_weight`` in 1-3."""
    if not isinstance(pb, dict):
        raise PlaybookValidationError("playbook must be a JSON object")
    if not isinstance(pb.get("version"), str) or not pb["version"].strip():
        raise PlaybookValidationError("playbook must carry a non-empty 'version' string")
    raw_positions = pb.get("positions")
    if not isinstance(raw_positions, list) or not raw_positions:
        raise PlaybookValidationError("'positions' must be a non-empty list")

    seen: set[str] = set()
    positions: list[Position] = []
    for i, p in enumerate(raw_positions):
        loc = f"positions[{i}]"
        if not isinstance(p, dict):
            raise PlaybookValidationError(f"{loc}: must be an object")
        ct = p.get("clause_type")
        if not (isinstance(ct, str) and ct.strip()):
            raise PlaybookValidationError(f"{loc}: 'clause_type' must be a non-empty string")
        if ct in seen:
            raise PlaybookValidationError(f"{loc}: duplicate clause_type {ct!r}")
        seen.add(ct)
        presence = p.get("presence")
        if presence not in _ALLOWED_PRESENCE:
            raise PlaybookValidationError(
                f"{loc} ({ct}): 'presence' must be one of {sorted(_ALLOWED_PRESENCE)}, "
                f"got {presence!r}"
            )
        weight = p.get("risk_weight")
        if not isinstance(weight, int) or isinstance(weight, bool) or not (
            _MIN_WEIGHT <= weight <= _MAX_WEIGHT
        ):
            raise PlaybookValidationError(
                f"{loc} ({ct}): 'risk_weight' must be an int in "
                f"[{_MIN_WEIGHT}, {_MAX_WEIGHT}], got {weight!r}"
            )
        standard = p.get("standard_position")
        if not (isinstance(standard, str) and standard.strip()):
            raise PlaybookValidationError(f"{loc} ({ct}): 'standard_position' must be a non-empty string")
        walk_away = p.get("walk_away") or ""
        positions.append(
            Position(
                clause_type=ct,
                presence=presence,
                risk_weight=weight,
                standard_position=standard,
                walk_away=str(walk_away),
            )
        )
    return positions


@dataclass(frozen=True)
class Playbook:
    version: str
    positions: tuple[Position, ...]


def load_playbook(path: str | Path = DEFAULT_PLAYBOOK_PATH) -> Playbook:
    """Read + validate the playbook JSON at ``path`` (defaults to the repo's single playbook
    file). Raises :class:`PlaybookValidationError` on a structurally broken file."""
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PlaybookValidationError(f"could not read/parse playbook at {p}: {exc}") from exc
    positions = validate(raw)
    return Playbook(version=str(raw["version"]), positions=tuple(positions))


@lru_cache
def get_playbook() -> Playbook:
    """Process-wide cached playbook, loaded + validated once (fails loudly at first use, which in
    practice is import time of ``agents.reviewer``/``agents.coverage`` — a broken playbook must
    never reach a live review)."""
    return load_playbook()


def positions_block(pb: Playbook | None = None) -> str:
    """Render the playbook as ONE prompt block: the reviewer's stable-prefix grounding text, listed
    clause by clause with its standard position and walk-away trigger."""
    pb = pb or get_playbook()
    lines = [f"PLAYBOOK (version {pb.version}) — our standard positions:"]
    for pos in pb.positions:
        lines.append(
            f"- {pos.clause_type} [{pos.presence}, risk weight {pos.risk_weight}]: "
            f"{pos.standard_position}"
            + (f" Walk-away: {pos.walk_away}" if pos.walk_away else "")
        )
    return "\n".join(lines)


def required_checklist(pb: Playbook | None = None) -> list[Position]:
    """The closed, deterministic checklist the coverage agent is handed: every position whose
    ``presence`` is ``"required"`` (deep mode only — plan §4.2)."""
    pb = pb or get_playbook()
    return [p for p in pb.positions if p.presence == "required"]
