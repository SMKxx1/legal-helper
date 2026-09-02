# Evaluation Governance — NDA Review Engine

The review engine's architecture is **benchmark-driven**: verdicts like "the whole-doc pass beats
per-clause fan-out (added ~0 recall at most of the cost — benchmark 2026-06-24)" are baked into
`app/engine/*.py`. Those verdicts are only trustworthy if we can **re-measure them on demand** and
**gate releases** on the result. This document defines the corpus, the gold-label schema, the
metrics and budgets, the release gates, and the drift-check cadence that make that possible.

The catastrophic failure class for legal review is a **false green**: a reviewer sees a green tier,
trusts it, and ships an agreement whose protections have been quietly stripped. Everything below is
organized around driving that class to zero.

---

## 1. Corpus

- **Location:** `backend/eval/corpus/`
  - `manifest.json` — the case list plus the governed thresholds.
  - `docs/` — the input documents (`.md`, realistic NDAs / non-NDAs).
  - `gold/` — one gold-label file per case.
- **Reports:** `backend/eval/reports/<utc-timestamp>.json` (git-ignored; attach to the PR).
- **Runner:** `backend/eval/run_eval.py` (offline validation by default; `--live` scores).

### Manifest (`manifest.json`)

```json
{
  "schema_version": 1,
  "thresholds": { "false_green_budget": 0, "must_find_recall_floor": 0.8 },
  "cases": [
    { "id": "hostile-edits", "doc": "docs/hostile_edits.md", "mode": "deep",
      "scope": "whole", "gold": "gold/hostile_edits.json", "notes": "..." }
  ]
}
```

Paths are relative to the manifest's directory. `mode` is `quick|deep`; `scope` is `whole`. The
**thresholds live in the manifest, not in code** — they are governed data that changes through the
same review as any other policy.

### Gold-label schema (`gold/*.json`)

```json
{
  "risk_tier_allowed": ["red", "yellow"],
  "false_green_budget_eligible": true,
  "expect_routing_flag": false,
  "must_find": [
    { "match": { "keywords": ["governing law", "cayman"], "kind": "finding" },
      "min_severity": "high" }
  ],
  "must_not": [
    { "match": { "keywords": ["some phrase"], "kind": "finding" } }
  ]
}
```

| Field | Meaning |
|-------|---------|
| `risk_tier_allowed` | Non-empty list of acceptable tiers (`green`/`yellow`/`red`). The case's tier check passes iff the engine's tier is in this list. If it omits `green`, green is *forbidden*. |
| `false_green_budget_eligible` | Whether this case counts toward the false-green budget. A clean doc where green is legitimate is set `false` so it never contributes a false green. |
| `must_find` | Expectations the engine must surface. Each has a `match` (`keywords` + `kind`) and, for `kind: finding`, a `min_severity` (`high`/`medium`). |
| `must_find[].match.kind` | `finding` → matched against finding text (title + rationale + span + clause heading + guidance); `absent` → matched against `coverage.absent_required` (item_key + clause_type + note). |
| `must_not` | *Optional.* Matches that should **not** appear (false-positive guard). Recorded in the report; not currently a hard gate. |
| `expect_routing_flag` | *Optional.* When `true`, the runner asserts routing flagged the doc: `routing.router.is_nda == false` **or** `routing.reasons` is non-empty. |

**Keyword matching** is case-insensitive substring, and **all** keywords in a `match` must land in a
**single** finding (or a single absent entry) — one finding's words can't be pooled with another's to
credit an expectation. This mirrors the clause-locality discipline in `app/eval_scoring.py`.

### The seed cases

| id | doc | mode | design |
|----|-----|------|--------|
| `clean-mutual` | reworded standard template | quick | Cosmetic rewording only. Green is correct → **not** false-green-eligible. Specificity anchor: if this starts going red, the engine is trigger-happy. |
| `hostile-edits` | 4 planted adverse edits | deep | Survival term slashed 2 yr → 3 mo; governing law Delaware → Cayman Islands; a one-way uncapped indemnity added; injunctive relief inverted into a waiver. Green forbidden; one `must_find` per edit at `high`. |
| `deleted-required-clause` | Return/Destruction clause removed | deep | An entire required clause deleted and sections renumbered. A whole-doc read can't see an absence — only **deep coverage** can — so this is `mode: deep` with an `absent` must_find. |
| `not-an-nda` | a services invoice | quick | Routing must flag it (`is_nda=false` or `reasons` non-empty) instead of a confident green. `expect_routing_flag: true`; green forbidden. |

### How to add a case (do this for every incident)

**Every production incident and every missed finding becomes a case.** That is how the corpus stays
honest: it accretes exactly the failures the engine has actually made.

1. Drop the (sanitized) document in `corpus/docs/`.
2. Author `corpus/gold/<id>.json` per the schema. Encode the *specific* thing that went wrong —
   the missed clause as a `must_find`, the wrongly-green verdict via `false_green_budget_eligible`
   + a `risk_tier_allowed` that omits green.
3. Add the case to `manifest.json`.
4. Run `make eval` (offline schema check) — it must pass. `test_eval_manifest.py` enforces this in CI.
5. Run `make eval-live` and confirm the engine catches it (fix the engine if it doesn't).

---

## 2. Metrics and budgets

Both thresholds are governed data in `manifest.json`.

- **False-green budget = 0.** A "false green" is any `false_green_budget_eligible` case whose gold
  forbids green but the engine returned green. This is the **catastrophic** class: a green tier is
  the one verdict a busy reviewer acts on without reading further, so a false green on a
  hostile-edits, deleted-clause, or not-an-NDA document ships an unprotected agreement. The budget is
  zero and the gate is hard.
- **must_find recall floor = 0.8.** Recall = (must_find expectations hit) / (total must_find
  expectations) across the whole corpus. Below the floor, the engine is missing too many known,
  material defects. This is a **recall** gate — precision (`must_not`) is tracked but not yet gating,
  because for legal review a missed adverse edit is far more damaging than a redundant flag.

Per-case tier checks and routing checks are recorded in every report but are **not** hard gates on
their own; they surface tier-mapping lenience (e.g. a deleted mandatory clause returning yellow
instead of red) as a visible per-case fail without failing the whole run.

---

## 3. Release gates

`make eval-live` **MUST run and pass**, with the report JSON attached to the PR, before merging any
change to:

- **Prompts** — the prompt constants live in `app/engine/*.py` (e.g. router / wholedoc / coverage /
  findings prompt text). Any wording change can move recall or the tier boundary.
- **Model ids or tiers** — the provider/model selection and quick/deep/effort wiring
  (`build_engine_gateways`, `effort=`, `profile=`). A model swap re-opens every benchmark verdict.
- **Playbooks / baselines** — `playbook/**` (v4 variant JSON, baselines, `playbook_nda_v3.json`).
  These change the checklist and standard positions the engine grades against; the report records
  `playbook_release_id()` so a run is attributable to an exact release.
- **Engine orchestration flags** — the flags in `routes_v1._run_engine` / `run_review`
  (`clause_pass`, `whole_doc`, `skip_coverage`, `cross_clause`, `self_verify`, `unchanged_threshold`,
  embedding signals, etc.).

**On benchmark verdicts.** Conclusions like "whole-doc beats per-clause fan-out" or "a max tier with
a second Opus read wasn't worth 2.5× the cost" are **model-generation-dependent**. The retired,
flag-gated passes (per-clause fan-out, cross-clause, ensemble self-verify) exist precisely so those
A/Bs can be re-run: when the model generation changes, flip the flag, run `eval-live` on both
configurations, and compare the reports before trusting the old verdict. Do not treat a dated
benchmark as permanent.

---

## 4. Drift checks

The engine can regress without any local change — a provider silently updates a model, or deprecates
one and routes you to a successor.

- **Cadence:** run `make eval-live` **monthly**, and **immediately on any provider model
  deprecation / forced migration**.
- **Comparison:** diff the new report against the previous one (`backend/eval/reports/`). Watch
  `aggregate.recall`, `gates.false_green.actual`, per-case `tier_pass`, and `total_cost_usd`. A drop
  in recall, any new false green, or a cost jump is a regression to investigate before it reaches
  production — even when every gate still nominally passes.
- Because reports are timestamped and record `playbook_release_id` (and, once available,
  `prompt_release` / model ids), month-over-month comparison is apples-to-apples.

---

## 5. Provenance

Each report records what produced it, so a result is attributable and reproducible:

- `generated_at_utc` — when the run happened.
- `playbook_release_id` — the 16-hex content hash of the resolved playbook release
  (`app/playbook/release.py`). A playbook edit changes this id.
- `thresholds` — the governed gate values in force for the run.
- Per-case `engine_mode`, `degraded_components`, `provenance` — captured **when the engine emits
  them** (the scorer reads every engine field defensively, so these appear automatically as the
  ReviewResult provenance/prompt-release/model-id fields land, without a runner change).

When prompt-release and model-id provenance become first-class fields on the ReviewResult / payload,
they flow into the report through the same defensive capture and should be cited in the PR alongside
`playbook_release_id`.

---

## 6. Running it

```bash
make eval        # offline: validate manifest + gold + docs (no network, no keys)
make eval-live   # live: run the corpus through the engine, enforce gates, write a report
```

`eval-live` requires a provider key (`OPENROUTER_API_KEY`, or the Anthropic key via
`settings_store.effective().anthropic_api_key`) and refuses with a clear message otherwise. It exits
non-zero when a gate fails. Offline validation is also enforced in CI by
`backend/tests/test_eval_manifest.py`.
