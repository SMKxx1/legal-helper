"""Deterministic, provider-neutral building blocks the review pipeline is built on.

Nothing here calls a model directly — model access goes through ``app.ai.gateway``. What remains
after the rebuild (plan §2.1):

    spans            — span faithfulness: verify a model-cited quote is a verbatim substring of
                        the document, and snap it to the document's exact text (LIVE — the
                        orchestrator's fabrication gate, ``app.agents.orchestrator._verify_span``)
    portable_schema   — the structured-output schema portability rules + D1 field-ordering check,
                        used by every schema in ``app.agents.schemas``

The classifier/reviewer/coverage agents and their orchestration live in ``app.agents``; this
package is their (schema/span) foundation, not the pipeline itself.
"""
