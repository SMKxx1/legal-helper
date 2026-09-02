"""Fast, offline guard for the eval corpus (docs/EVALUATION.md).

Proves the governed corpus is well-formed WITHOUT any network or engine run: the manifest
parses, every referenced document and gold file exists, every gold file matches the schema,
and the release gates (false-green budget + must_find recall floor) are present as governed
data. If someone adds a case with a dangling path or a malformed gold file, this fails in CI
long before ``make eval-live`` would.
"""

from __future__ import annotations

from eval.run_eval import (
    _SEVERITY_RANK,
    _VALID_KINDS,
    _VALID_TIERS,
    CORPUS_DIR,
    MANIFEST_PATH,
    validate_corpus,
)


def test_manifest_and_gold_are_well_formed() -> None:
    manifest, cases, errors = validate_corpus()
    assert not errors, f"corpus validation errors: {errors}"
    assert cases, "expected at least one case"


def test_thresholds_present_as_governed_data() -> None:
    manifest, _cases, _errors = validate_corpus()
    thresholds = manifest["thresholds"]
    assert isinstance(thresholds["false_green_budget"], int)
    floor = thresholds["must_find_recall_floor"]
    assert 0.0 <= float(floor) <= 1.0


def test_manifest_exists() -> None:
    assert MANIFEST_PATH.exists(), f"manifest missing at {MANIFEST_PATH}"


def test_every_doc_and_gold_path_exists() -> None:
    _manifest, cases, _errors = validate_corpus()
    for case in cases:
        assert case["doc_path"] is not None and case["doc_path"].exists(), (
            f"{case['id']}: doc missing"
        )
        assert case["gold"], f"{case['id']}: gold missing or empty"


def test_gold_schema_shape() -> None:
    _manifest, cases, _errors = validate_corpus()
    for case in cases:
        gold = case["gold"]
        assert gold["risk_tier_allowed"], f"{case['id']}: empty risk_tier_allowed"
        assert all(t in _VALID_TIERS for t in gold["risk_tier_allowed"])
        assert isinstance(gold["false_green_budget_eligible"], bool)
        for mf in gold["must_find"]:
            match = mf["match"]
            assert match["keywords"], f"{case['id']}: empty must_find keywords"
            assert match["kind"] in _VALID_KINDS
            assert mf.get("min_severity", "medium") in _SEVERITY_RANK


def test_expected_seed_cases_present() -> None:
    _manifest, cases, _errors = validate_corpus()
    ids = {c["id"] for c in cases}
    assert {
        "clean-mutual",
        "hostile-edits",
        "deleted-required-clause",
        "not-an-nda",
    } <= ids

    by_id = {c["id"]: c for c in cases}
    # The clean case must NOT be in the false-green budget (green is a legitimate verdict).
    assert by_id["clean-mutual"]["gold"]["false_green_budget_eligible"] is False
    # The adversarial cases MUST be in the false-green budget and forbid green.
    for cid in ("hostile-edits", "deleted-required-clause", "not-an-nda"):
        gold = by_id[cid]["gold"]
        assert gold["false_green_budget_eligible"] is True, cid
        assert "green" not in gold["risk_tier_allowed"], cid
    # The deleted-clause case is deep (only coverage can see an absence) and asserts an absent match.
    deleted = by_id["deleted-required-clause"]
    assert deleted["mode"] == "deep"
    assert any(mf["match"]["kind"] == "absent" for mf in deleted["gold"]["must_find"])
    # The not-an-NDA case relies on the routing flag.
    assert by_id["not-an-nda"]["gold"]["expect_routing_flag"] is True


def test_corpus_dir_layout() -> None:
    assert (CORPUS_DIR / "docs").is_dir()
    assert (CORPUS_DIR / "gold").is_dir()
