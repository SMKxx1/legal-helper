"""Checklist-driven coverage (improvement A) — the recall win on false-GREEN-by-omission.

The required-clause list is derived **deterministically in code** from the
playbook. The model is then handed a *closed* checklist and only answers
present/absent + span per item — it never has to reason open-endedly about
"what's missing", which is exactly what makes a ``fast`` model sufficient for
T1.6 (decision #2).

A clause is "required-present" when its playbook position says so
(``presence == "required"``); absent that field we fall back to a documented
core set. ``required_carveouts`` are expanded into one checklist item each, so a
single missing carve-out (e.g. independent-development) is caught on its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

#: Documented fallback when the playbook has no explicit per-clause ``presence``.
#: These clause types should be present in a conforming NDA; absence is a finding.
DEFAULT_REQUIRED_PRESENT: frozenset[str] = frozenset(
    {
        "confidentiality_definition",
        "permitted_purpose",
        "exclusions_carveouts",
        "obligations_of_confidentiality",
        "term_of_confidentiality",
        "return_destruction",
        "governing_law_jurisdiction",
    }
)

#: Fallback red-flag set (should be ABSENT from an NDA; flagged by T2 when present).
#: Used for T2/T4, not the coverage pass. Derived from playbook risk_weight when available.
PROHIBITED_DEFAULT: frozenset[str] = frozenset(
    {
        "non_solicitation",
        "non_competition",
        "residuals",
        "indemnification",
    }
)

_HIGH_RISK_WEIGHT = 5

#: Top-level keys every playbook must carry. A silent typo in any of these (or in a
#: per-position field) would drop clauses from ``required_clause_types`` — and therefore
#: from deep's coverage checklist (the deleted-clause net) — with no error anywhere, so we
#: fail loudly at load instead. Ranges/enums are derived from the shipped ``playbook/v4/*``
#: corpus (presence ∈ {required, optional, prohibited}; risk_weight ∈ [2, 5]).
#:
#: ``variant`` / ``variant_key`` are v4-only; the legacy v3 fallback playbook
#: (``playbook_nda_v3.json``) predates the per-variant split and carries neither, so they're
#: enforced only when the file self-identifies as v4 (has ``variant_key``). The remaining
#: keys are common to both shapes.
_REQUIRED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {"contract_type", "version_no", "status", "defaults", "positions"}
)
_V4_ONLY_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"variant", "variant_key"})
_PRESENCE_ENUM: frozenset[str] = frozenset({"required", "optional", "prohibited"})
_RISK_WEIGHT_MIN = 2
_RISK_WEIGHT_MAX = 5


class PlaybookValidationError(ValueError):
    """A playbook (or the v4 manifest) is structurally invalid. Raised at load /
    variant-selection time so a typo can't silently drop a clause from the checklist."""


def _validate_playbook(pb: dict, where: str) -> None:
    """Fail loudly on a structurally invalid playbook. Unknown extra keys stay allowed;
    only the required shape is enforced. ``where`` is a source hint for the message."""
    if not isinstance(pb, dict):
        raise PlaybookValidationError(f"{where}: playbook must be a JSON object")
    # A v4 playbook self-identifies via ``variant_key``; when present, the paired
    # ``variant`` key is required too (a half-declared variant is a typo, not a v3 file).
    is_v4 = "variant_key" in pb or "variant" in pb
    required_top = _REQUIRED_TOP_LEVEL_KEYS
    if is_v4:
        required_top = required_top | _V4_ONLY_TOP_LEVEL_KEYS
    missing = sorted(required_top - pb.keys())
    if missing:
        raise PlaybookValidationError(f"{where}: missing top-level keys {missing}")
    if not isinstance(pb.get("defaults"), dict):
        raise PlaybookValidationError(f"{where}: 'defaults' must be an object")
    positions = pb.get("positions")
    if not isinstance(positions, list) or not positions:
        raise PlaybookValidationError(f"{where}: 'positions' must be a non-empty list")
    for i, p in enumerate(positions):
        loc = f"{where}: positions[{i}]"
        if not isinstance(p, dict):
            raise PlaybookValidationError(f"{loc}: must be an object")
        ct = p.get("clause_type")
        if not (isinstance(ct, str) and ct.strip()):
            raise PlaybookValidationError(
                f"{loc}: 'clause_type' must be a non-empty str"
            )
        loc = f"{where}: position '{ct}'"
        if "title" in p and not isinstance(p["title"], str):
            raise PlaybookValidationError(f"{loc}: 'title' must be a str")
        sp = p.get("standard_position")
        if not (isinstance(sp, str) and sp.strip()):
            raise PlaybookValidationError(
                f"{loc}: 'standard_position' must be a non-empty str"
            )
        rw = p.get("risk_weight")
        # bool is an int subclass — reject it explicitly so True/False can't pose as a weight.
        if (
            not isinstance(rw, int)
            or isinstance(rw, bool)
            or not (_RISK_WEIGHT_MIN <= rw <= _RISK_WEIGHT_MAX)
        ):
            raise PlaybookValidationError(
                f"{loc}: 'risk_weight' must be an int in "
                f"[{_RISK_WEIGHT_MIN}, {_RISK_WEIGHT_MAX}], got {rw!r}"
            )
        # ``presence`` is OPTIONAL — absent falls back to the documented core set in
        # ``required_clause_types`` (the legacy v3 playbook omits it entirely). But a
        # PRESENT value must be in the enum: a typo like "requird" would otherwise silently
        # drop that clause from the required set (and thus the coverage checklist).
        if "presence" in p and p["presence"] not in _PRESENCE_ENUM:
            raise PlaybookValidationError(
                f"{loc}: 'presence' must be one of {sorted(_PRESENCE_ENUM)}, "
                f"got {p['presence']!r}"
            )
        # v4 uses plain str entries here; the legacy v3 playbook uses richer
        # ``{position, evidence_count}`` objects. Enforce list-of-str for v4 (the shape the
        # task specifies) but only "a list" for v3 so the legacy file keeps loading.
        for lk in ("walk_away_triggers", "acceptable_fallbacks"):
            if lk not in p:
                continue
            v = p[lk]
            ok = isinstance(v, list) and (
                not is_v4 or all(isinstance(x, str) for x in v)
            )
            if not ok:
                kind = "list of str" if is_v4 else "list"
                raise PlaybookValidationError(f"{loc}: '{lk}' must be a {kind}")


@dataclass(frozen=True)
class ChecklistItem:
    key: str  # stable id, e.g. "clause:term_of_confidentiality" / "carveout:public"
    clause_type: str
    label: str
    required_position: (
        str  # the playbook standard_position (for the prompt + the report)
    )
    kind: str  # "clause" | "carveout"


def load_playbook(path: str | Path) -> dict:
    pb = json.loads(Path(path).read_text())
    _validate_playbook(pb, str(path))
    return pb


def validate_v4_manifest(man: dict, repo_root: str | Path) -> None:
    """Fail loudly on a structurally inconsistent v4 manifest, at variant-selection time.

    Enforces: entries carry every ``selection_axes`` key plus ``variant_key`` /
    ``playbook`` / ``baseline``, and every referenced playbook + baseline file exists on
    disk under ``repo_root``. A missing baseline (or a playbook path typo) would otherwise
    only surface as a per-review 503 much later — here it's caught up front.
    """
    root = Path(repo_root)
    if not isinstance(man, dict):
        raise PlaybookValidationError("manifest: must be a JSON object")
    axes = man.get("selection_axes")
    if not (isinstance(axes, list) and all(isinstance(a, str) for a in axes)):
        raise PlaybookValidationError(
            "manifest: 'selection_axes' must be a list of str"
        )
    entries = man.get("playbooks")
    if not isinstance(entries, list) or not entries:
        raise PlaybookValidationError("manifest: 'playbooks' must be a non-empty list")
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise PlaybookValidationError(f"manifest: playbooks[{i}] must be an object")
        vk = e.get("variant_key")
        if not (isinstance(vk, str) and vk.strip()):
            raise PlaybookValidationError(
                f"manifest: playbooks[{i}] 'variant_key' must be a non-empty str"
            )
        loc = f"manifest: entry '{vk}'"
        missing_axes = [a for a in axes if not e.get(a)]
        if missing_axes:
            raise PlaybookValidationError(
                f"{loc}: missing selection-axis values {missing_axes}"
            )
        for ref in ("playbook", "baseline"):
            rel = e.get(ref)
            if not (isinstance(rel, str) and rel.strip()):
                raise PlaybookValidationError(f"{loc}: '{ref}' must be a non-empty str")
            if not (root / rel).exists():
                raise PlaybookValidationError(
                    f"{loc}: {ref} file not found at {root / rel}"
                )


def _positions(pb: dict) -> dict[str, dict]:
    return {
        p.get("clause_type"): p for p in pb.get("positions", []) if p.get("clause_type")
    }


def required_clause_types(pb: dict) -> set[str]:
    """Clause types that must be present. Explicit ``presence: required`` wins;
    otherwise the documented default set."""
    pos = _positions(pb)
    explicit = {ct for ct, p in pos.items() if p.get("presence") == "required"}
    return explicit if explicit else set(DEFAULT_REQUIRED_PRESENT)


def prohibited_clause_types(pb: dict) -> set[str]:
    """Clause types that should NOT appear (red flags). Explicit
    ``presence: prohibited`` wins; else high-``risk_weight`` positions; else default."""
    pos = _positions(pb)
    explicit = {ct for ct, p in pos.items() if p.get("presence") == "prohibited"}
    if explicit:
        return explicit
    high = {
        ct
        for ct, p in pos.items()
        if isinstance(p.get("risk_weight"), int)
        and p["risk_weight"] >= _HIGH_RISK_WEIGHT
    }
    return high or set(PROHIBITED_DEFAULT)


def _carveout_key(entry: str) -> str:
    # "public (information ...)" -> "public"
    return entry.split("(")[0].strip()


def build_checklist(pb: dict) -> list[ChecklistItem]:
    """The closed checklist the coverage model fills (present/absent + span)."""
    pos = _positions(pb)
    carveouts = pb.get("defaults", {}).get("required_carveouts", []) or []
    items: list[ChecklistItem] = []
    for ct in sorted(required_clause_types(pb)):
        # When carve-outs are declared they are expanded into per-carve-out items below; skip the
        # single exclusions_carveouts clause item here so it isn't duplicated.
        if ct == "exclusions_carveouts" and carveouts:
            continue
        p = pos.get(ct, {})
        items.append(
            ChecklistItem(
                f"clause:{ct}",
                ct,
                ct.replace("_", " "),
                p.get("standard_position", ""),
                "clause",
            )
        )
    # Required carve-outs are ALWAYS expanded when declared — INDEPENDENT of whether
    # exclusions_carveouts landed in the required-clause set. An explicit presence:required playbook
    # that doesn't mark it would otherwise silently drop every required carve-out from the checklist.
    if carveouts:
        std = pos.get("exclusions_carveouts", {}).get("standard_position", "")
        seen_keys: dict[str, int] = {}
        for entry in carveouts:
            base = _carveout_key(entry)
            # Two carve-outs sharing a leading token ("public (x)" / "public (y)") would derive the
            # SAME key and collapse in by_key — disambiguate with an index so each is scored once.
            n = seen_keys.get(base, 0)
            seen_keys[base] = n + 1
            key = base if n == 0 else f"{base}_{n}"
            items.append(
                ChecklistItem(
                    f"carveout:{key}",
                    "exclusions_carveouts",
                    f"carve-out: {entry}",
                    std,
                    "carveout",
                )
            )
    return items


def checklist_from_file(path: str | Path) -> list[ChecklistItem]:
    return build_checklist(load_playbook(path))
