"""Load-time validation for playbooks + the v4 manifest (recall-safety net).

A typo like ``presence: "requird"`` used to silently drop that clause from
``required_clause_types`` and therefore from deep's coverage checklist (the deleted-clause
net) with no error anywhere. ``load_playbook`` / ``validate_v4_manifest`` now fail loudly.
These tests lock that: all shipped files pass unchanged, and each malformed shape raises.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.playbook.coverage import (
    PlaybookValidationError,
    load_playbook,
    validate_v4_manifest,
)

_REPO = Path(__file__).resolve().parents[2]  # tests/ -> backend/ -> repo
_V4 = _REPO / "playbook" / "v4"
_MANIFEST = _V4 / "manifest.json"
_PLAYBOOKS = sorted(p for p in _V4.glob("*.json") if p.name != "manifest.json")
# The legacy v3 fallback playbook is still served by ``load_playbook`` (with scoped leniency —
# it omits ``required_clause_types`` / ``variant_key``), so validation tightening must not
# silently start rejecting it.
_V3_PLAYBOOK = _REPO / "playbook" / "playbook_nda_v3.json"


def _sample_playbook() -> dict:
    return load_playbook(_PLAYBOOKS[0])


# --------------------------------------------------------------------------- #
# Zero behavior change: every shipped file must pass unchanged.
# --------------------------------------------------------------------------- #
def test_all_v4_playbooks_pass() -> None:
    assert _PLAYBOOKS, "no v4 playbook files found — corpus path wrong?"
    for path in _PLAYBOOKS:
        load_playbook(path)  # must not raise


def test_legacy_v3_fallback_playbook_passes() -> None:
    """The v3 fallback is still a live source (load_playbook serves it with scoped leniency);
    lock it into validation so future tightening can't silently reject it."""
    assert _V3_PLAYBOOK.exists(), f"v3 fallback playbook missing at {_V3_PLAYBOOK}"
    load_playbook(_V3_PLAYBOOK)  # must not raise


def test_manifest_passes() -> None:
    man = json.loads(_MANIFEST.read_text())
    validate_v4_manifest(man, _REPO)  # must not raise


# --------------------------------------------------------------------------- #
# Playbook validation failures.
# --------------------------------------------------------------------------- #
def test_bad_presence_value_rejected() -> None:
    pb = _sample_playbook()
    pb["positions"][0]["presence"] = "requird"  # the motivating typo
    with pytest.raises(PlaybookValidationError, match="presence"):
        _write_and_load(pb)


def test_missing_standard_position_rejected() -> None:
    pb = _sample_playbook()
    del pb["positions"][0]["standard_position"]
    with pytest.raises(PlaybookValidationError, match="standard_position"):
        _write_and_load(pb)


def test_risk_weight_as_string_rejected() -> None:
    pb = _sample_playbook()
    pb["positions"][0]["risk_weight"] = "5"
    with pytest.raises(PlaybookValidationError, match="risk_weight"):
        _write_and_load(pb)


def test_empty_positions_rejected() -> None:
    pb = _sample_playbook()
    pb["positions"] = []
    with pytest.raises(PlaybookValidationError, match="positions"):
        _write_and_load(pb)


def _write_and_load(pb: dict) -> dict:
    # write to a throwaway path so load_playbook exercises the real read+validate path
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(pb, fh)
        path = fh.name
    return load_playbook(path)


# --------------------------------------------------------------------------- #
# Manifest validation failure: references a missing baseline file.
# --------------------------------------------------------------------------- #
def test_manifest_missing_baseline_rejected() -> None:
    man = json.loads(_MANIFEST.read_text())
    man = copy.deepcopy(man)
    man["playbooks"][0]["baseline"] = "playbook/v4/baselines/DOES_NOT_EXIST.md"
    with pytest.raises(PlaybookValidationError, match="not found"):
        validate_v4_manifest(man, _REPO)
