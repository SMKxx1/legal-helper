"""Channel-agnostic review engine.

Deterministic, provider-neutral building blocks for the review pipeline. Nothing here calls a model
directly — model access goes through ``app.ai.gateway`` (the provider boundary).

The full request flow + which file owns each tier (and which are LIVE vs eval-only) is mapped in
``docs/ARCHITECTURE.md`` ("The engine package map"). In short:
    review_service.run_review  — orchestrator (LIVE)
    router                     — T0 classify + pick playbook variant (LIVE)
    wholedoc                   — whole-document structured review, the sole finding source (LIVE)
    coverage_runner            — detect a deleted/absent required clause (LIVE, deep tier)
    simcache / spans / portable_schema — dedup cache / span faithfulness / shared finding schema (LIVE)
    findings / verify / crossclause / walkaway — eval-only passes (reachable only via flags prod never sets)
"""
