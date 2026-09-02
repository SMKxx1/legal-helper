"""Evaluation runner for the NDA review engine — offline validation + --live scoring.

Two modes:

* **offline** (default): validate the manifest, gold files, and documents — every referenced
  path exists, every gold file matches the schema, and the governed thresholds are present.
  No provider keys, no network, no engine imports. Exits 0 when the corpus is well-formed.

* **--live**: additionally run every case through ``app.api.routes_v1._run_engine`` and score it.
  Requires a provider key (``effective().anthropic_api_key`` or ``OPENROUTER_API_KEY``); refuses
  with a clear message otherwise. Writes a timestamped JSON report to ``backend/eval/reports/``
  and exits non-zero if either gate fails.

Gates (thresholds live in the manifest so they are governed data, not code):

* **false-green budget** — the count of ``false_green_budget_eligible`` cases whose gold forbids
  green but the engine returned green must be ``<= false_green_budget`` (default 0). A false green
  on a hostile/deleted-clause/out-of-scope document is the catastrophic failure class for legal
  review: a reviewer trusts a green and ships an unprotected agreement.
* **must_find recall** — fraction of all ``must_find`` expectations the engine hit, across the whole
  corpus, must be ``>= must_find_recall_floor`` (default 0.8).

Invocation (from ``backend/`` so ``pythonpath=.`` applies):
    python -m eval.run_eval            # offline validation
    python -m eval.run_eval --live     # live scoring (needs a provider key)
    python eval/run_eval.py            # path invocation also works (sys.path bootstrap below)

The scorer reads the engine result defensively (``getattr``/``.get``) so a parallel work stream
adding or removing optional ReviewResult fields (mode, degraded_components, provenance) never
breaks the run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# --- path bootstrap: make `app` importable under BOTH `python -m eval.run_eval` (cwd=backend,
# already on path) and a bare `python eval/run_eval.py` (sys.path[0] is eval/, not backend/). ---
_EVAL_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _EVAL_DIR.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

CORPUS_DIR = _EVAL_DIR / "corpus"
MANIFEST_PATH = CORPUS_DIR / "manifest.json"
REPORTS_DIR = _EVAL_DIR / "reports"

_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
_VALID_TIERS = {"green", "yellow", "red"}
_VALID_MODES = {"quick", "deep"}
_VALID_KINDS = {"finding", "absent"}


# --------------------------------------------------------------------------- #
# Schema validation (offline; no engine, no network)
# --------------------------------------------------------------------------- #
def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _validate_gold(gold: Any, where: str, errors: list[str]) -> None:
    if not isinstance(gold, dict):
        errors.append(f"{where}: gold must be a JSON object")
        return
    tiers = gold.get("risk_tier_allowed")
    if not isinstance(tiers, list) or not tiers:
        errors.append(f"{where}: risk_tier_allowed must be a non-empty list")
    else:
        bad = [t for t in tiers if t not in _VALID_TIERS]
        if bad:
            errors.append(f"{where}: risk_tier_allowed has invalid tiers {bad}")
    if not isinstance(gold.get("false_green_budget_eligible"), bool):
        errors.append(f"{where}: false_green_budget_eligible must be a bool")
    must_find = gold.get("must_find")
    if not isinstance(must_find, list):
        errors.append(f"{where}: must_find must be a list")
    else:
        for i, mf in enumerate(must_find):
            _validate_matcher(mf, f"{where}.must_find[{i}]", errors, require_sev=True)
    must_not = gold.get("must_not", [])
    if not isinstance(must_not, list):
        errors.append(f"{where}: must_not must be a list when present")
    else:
        for i, mn in enumerate(must_not):
            _validate_matcher(mn, f"{where}.must_not[{i}]", errors, require_sev=False)
    if "expect_routing_flag" in gold and not isinstance(
        gold["expect_routing_flag"], bool
    ):
        errors.append(f"{where}: expect_routing_flag must be a bool when present")


def _validate_matcher(
    entry: Any, where: str, errors: list[str], *, require_sev: bool
) -> None:
    if not isinstance(entry, dict):
        errors.append(f"{where}: must be an object")
        return
    match = entry.get("match")
    if not isinstance(match, dict):
        errors.append(f"{where}.match: must be an object")
        return
    kws = match.get("keywords")
    if not isinstance(kws, list) or not kws or not all(isinstance(k, str) for k in kws):
        errors.append(f"{where}.match.keywords: must be a non-empty list of strings")
    kind = match.get("kind")
    if kind not in _VALID_KINDS:
        errors.append(f"{where}.match.kind: must be one of {sorted(_VALID_KINDS)}")
    if require_sev:
        sev = entry.get("min_severity", "medium")
        if sev not in _SEVERITY_RANK:
            errors.append(
                f"{where}.min_severity: must be one of {sorted(_SEVERITY_RANK)}"
            )


def validate_corpus() -> tuple[dict, list[dict], list[str]]:
    """Validate the manifest + gold + docs. Returns (manifest, resolved_cases, errors)."""
    errors: list[str] = []
    if not MANIFEST_PATH.exists():
        return {}, [], [f"manifest not found at {MANIFEST_PATH}"]
    try:
        manifest = _load_json(MANIFEST_PATH)
    except (ValueError, OSError) as exc:
        return {}, [], [f"manifest is not valid JSON: {exc}"]

    if not isinstance(manifest, dict):
        return {}, [], ["manifest must be a JSON object"]
    if "schema_version" not in manifest:
        errors.append("manifest: missing schema_version")

    thresholds = manifest.get("thresholds")
    if not isinstance(thresholds, dict):
        errors.append("manifest: thresholds must be an object")
    else:
        if not isinstance(thresholds.get("false_green_budget"), int):
            errors.append("manifest.thresholds: false_green_budget must be an int")
        floor = thresholds.get("must_find_recall_floor")
        if not isinstance(floor, (int, float)) or not (0.0 <= float(floor) <= 1.0):
            errors.append(
                "manifest.thresholds: must_find_recall_floor must be a number in [0, 1]"
            )

    cases = manifest.get("cases")
    resolved: list[dict] = []
    if not isinstance(cases, list) or not cases:
        errors.append("manifest: cases must be a non-empty list")
        return manifest, resolved, errors

    seen_ids: set[str] = set()
    for i, case in enumerate(cases):
        where = f"cases[{i}]"
        if not isinstance(case, dict):
            errors.append(f"{where}: must be an object")
            continue
        cid = case.get("id")
        if not isinstance(cid, str) or not cid:
            errors.append(f"{where}: id must be a non-empty string")
            cid = where
        if cid in seen_ids:
            errors.append(f"{where}: duplicate case id {cid!r}")
        seen_ids.add(cid)
        if case.get("mode") not in _VALID_MODES:
            errors.append(f"{cid}: mode must be one of {sorted(_VALID_MODES)}")
        if case.get("scope") != "whole":
            errors.append(f"{cid}: scope must be 'whole'")

        doc_rel = case.get("doc")
        doc_path = CORPUS_DIR / doc_rel if isinstance(doc_rel, str) else None
        if not doc_path or not doc_path.exists():
            errors.append(f"{cid}: doc not found ({doc_rel!r})")

        gold_rel = case.get("gold")
        gold_path = CORPUS_DIR / gold_rel if isinstance(gold_rel, str) else None
        gold_obj: dict = {}
        if not gold_path or not gold_path.exists():
            errors.append(f"{cid}: gold not found ({gold_rel!r})")
        else:
            try:
                gold_obj = _load_json(gold_path)
            except (ValueError, OSError) as exc:
                errors.append(f"{cid}: gold is not valid JSON: {exc}")
            else:
                _validate_gold(gold_obj, cid, errors)

        resolved.append(
            {
                "id": cid,
                "mode": case.get("mode"),
                "scope": case.get("scope"),
                "doc_path": doc_path,
                "gold": gold_obj,
                "notes": case.get("notes", ""),
            }
        )
    return manifest, resolved, errors


# --------------------------------------------------------------------------- #
# Scoring helpers (pure; no engine dependency)
# --------------------------------------------------------------------------- #
def _finding_text(f: dict) -> str:
    parts = [
        f.get("title", ""),
        f.get("rationale", ""),
        f.get("span", ""),
        f.get("clause_heading", ""),
        f.get("guidance", ""),
    ]
    return " ".join(str(p) for p in parts).lower()


def _finding_sev(f: dict) -> str:
    return f.get("verified_severity") or f.get("severity") or "low"


def _absent_text(a: dict) -> str:
    parts = [a.get("item_key", ""), a.get("clause_type", ""), a.get("note", "")]
    return " ".join(str(p) for p in parts).lower()


def _keywords_in(text: str, keywords: list[str]) -> bool:
    return all(k.lower() in text for k in keywords)


def _matcher_hit(matcher: dict, findings: list[dict], absent: list[dict]) -> bool:
    match = matcher.get("match", {})
    keywords = match.get("keywords", [])
    kind = match.get("kind")
    if kind == "absent":
        return any(_keywords_in(_absent_text(a), keywords) for a in absent)
    # kind == "finding": all keywords in one finding AND that finding meets min_severity.
    min_rank = _SEVERITY_RANK.get(matcher.get("min_severity", "medium"), 2)
    for f in findings:
        if _keywords_in(_finding_text(f), keywords) and (
            _SEVERITY_RANK.get(_finding_sev(f), 1) >= min_rank
        ):
            return True
    return False


# --------------------------------------------------------------------------- #
# Live scoring
# --------------------------------------------------------------------------- #
def _detect_provider_key() -> str:
    """Return a non-empty provider key or '' when none is configured."""
    key = os.environ.get("OPENROUTER_API_KEY", "") or ""
    if key:
        return key
    try:
        from app.settings_store import effective

        return getattr(effective(), "anthropic_api_key", "") or ""
    except Exception:  # noqa: BLE001 — a missing/broken config is just "no key"
        return ""


def _score_case(case: dict, result: Any) -> dict:
    """Score one engine result against its gold. Reads ``result`` defensively."""
    gold = case["gold"]
    findings = list(getattr(result, "findings", []) or [])
    coverage = getattr(result, "coverage", None)
    absent_objs = getattr(coverage, "absent_required", []) or [] if coverage else []
    absent = [
        {
            "item_key": getattr(a, "item_key", ""),
            "clause_type": getattr(a, "clause_type", ""),
            "note": getattr(a, "note", ""),
        }
        for a in absent_objs
    ]
    risk_tier = getattr(result, "risk_tier", None)
    routing = getattr(result, "routing", None) or {}

    tier_allowed = gold.get("risk_tier_allowed", [])
    tier_pass = risk_tier in tier_allowed if tier_allowed else None

    fg_eligible = bool(gold.get("false_green_budget_eligible"))
    forbids_green = "green" not in tier_allowed
    false_green = fg_eligible and forbids_green and risk_tier == "green"

    must_find_results = []
    for mf in gold.get("must_find", []):
        must_find_results.append(
            {
                "keywords": mf.get("match", {}).get("keywords", []),
                "kind": mf.get("match", {}).get("kind"),
                "min_severity": mf.get("min_severity", "medium"),
                "hit": _matcher_hit(mf, findings, absent),
            }
        )

    must_not_results = []
    for mn in gold.get("must_not", []):
        must_not_results.append(
            {
                "keywords": mn.get("match", {}).get("keywords", []),
                "kind": mn.get("match", {}).get("kind"),
                "violated": _matcher_hit(
                    {**mn, "min_severity": mn.get("min_severity", "low")},
                    findings,
                    absent,
                ),
            }
        )

    routing_expected = bool(gold.get("expect_routing_flag"))
    routing_flag_ok: bool | None = None
    if routing_expected:
        is_nda = (routing.get("router") or {}).get("is_nda", True)
        reasons = routing.get("reasons") or []
        routing_flag_ok = (is_nda is False) or bool(reasons)

    return {
        "id": case["id"],
        "mode": case["mode"],
        "scope": case["scope"],
        "risk_tier": risk_tier,
        "risk_tier_allowed": tier_allowed,
        "tier_pass": tier_pass,
        "false_green_eligible": fg_eligible,
        "false_green_violation": false_green,
        "routing_flag_expected": routing_expected,
        "routing_flag_ok": routing_flag_ok,
        "must_find": must_find_results,
        "must_not": must_not_results,
        "n_findings": len(findings),
        "n_absent": len(absent),
        "cost_usd": getattr(result, "cost_usd", 0.0),
        # Provenance / mode fields, captured when the engine emits them (defensive).
        "engine_mode": getattr(result, "mode", None),
        "degraded_components": getattr(result, "degraded_components", None),
        "provenance": getattr(result, "provenance", None),
    }


def run_live(manifest: dict, cases: list[dict]) -> dict:
    """Run every case through the engine and build the scored report dict."""
    from app.api.routes_v1 import _run_engine
    from app.playbook.release import playbook_release_id

    case_reports: list[dict] = []
    for case in cases:
        text = case["doc_path"].read_text(encoding="utf-8")
        result = _run_engine(
            text,
            mode=case["mode"],
            playbook_version=None,
            scope=case["scope"],
        )
        case_reports.append(_score_case(case, result))

    thresholds = manifest.get("thresholds", {})
    fg_budget = int(thresholds.get("false_green_budget", 0))
    recall_floor = float(thresholds.get("must_find_recall_floor", 0.8))

    mf_total = sum(len(c["must_find"]) for c in case_reports)
    mf_hits = sum(1 for c in case_reports for mf in c["must_find"] if mf["hit"])
    recall = (mf_hits / mf_total) if mf_total else 1.0
    fg_count = sum(1 for c in case_reports if c["false_green_violation"])
    routing_checks = [c for c in case_reports if c["routing_flag_expected"]]
    routing_pass = sum(1 for c in routing_checks if c["routing_flag_ok"])

    fg_pass = fg_count <= fg_budget
    recall_pass = recall >= recall_floor
    overall_pass = fg_pass and recall_pass

    return {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "run_mode": "live",
        "playbook_release_id": playbook_release_id(),
        "thresholds": thresholds,
        "cases": case_reports,
        "aggregate": {
            "n_cases": len(case_reports),
            "must_find_total": mf_total,
            "must_find_hits": mf_hits,
            "recall": round(recall, 4),
            "false_green_count": fg_count,
            "tier_pass_count": sum(1 for c in case_reports if c["tier_pass"]),
            "routing_checks": len(routing_checks),
            "routing_pass": routing_pass,
            "total_cost_usd": round(
                sum(float(c["cost_usd"] or 0.0) for c in case_reports), 6
            ),
        },
        "gates": {
            "false_green": {
                "budget": fg_budget,
                "actual": fg_count,
                "pass": fg_pass,
            },
            "must_find_recall": {
                "floor": recall_floor,
                "actual": round(recall, 4),
                "pass": recall_pass,
            },
            "overall_pass": overall_pass,
        },
    }


def _write_report(report: dict) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NDA review-engine eval runner.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run cases through the engine (needs a provider key) and enforce gates.",
    )
    args = parser.parse_args(argv)

    manifest, cases, errors = validate_corpus()
    if errors:
        print("Corpus validation FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"Corpus OK: {len(cases)} case(s) validated against the gold schema.")

    if not args.live:
        print("Offline validation passed (no engine run). Use --live to score.")
        return 0

    key = _detect_provider_key()
    if not key:
        print(
            "--live requires a provider key. Set OPENROUTER_API_KEY or configure the "
            "Anthropic key (settings_store.effective().anthropic_api_key). Refusing to run.",
            file=sys.stderr,
        )
        return 2

    report = run_live(manifest, cases)
    path = _write_report(report)
    agg = report["aggregate"]
    gates = report["gates"]
    print(f"\nReport written: {path}")
    print(f"playbook_release_id: {report['playbook_release_id']}")
    print(
        f"must_find recall: {agg['must_find_hits']}/{agg['must_find_total']} "
        f"= {gates['must_find_recall']['actual']} "
        f"(floor {gates['must_find_recall']['floor']}) -> "
        f"{'PASS' if gates['must_find_recall']['pass'] else 'FAIL'}"
    )
    print(
        f"false-green: {gates['false_green']['actual']} "
        f"(budget {gates['false_green']['budget']}) -> "
        f"{'PASS' if gates['false_green']['pass'] else 'FAIL'}"
    )
    if not gates["overall_pass"]:
        print("\nGATES FAILED.", file=sys.stderr)
        return 3
    print("\nAll gates passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
