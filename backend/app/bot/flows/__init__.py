"""Bot cross-cutting flows — multi-step sequences that span the intake and reply planes.

An *intent* is a single ``(IntentContext) -> IntentReply`` turn; a *flow* is a longer sequence stitched
across surfaces. The generate feature is exactly this shape: the ``generate`` intent hands the user a
Tally form link (channel pre-filled) and returns; then — separately, when the user SUBMITS the Tally
form — the Tally webhook (``app.api.routes_tally``) maps the fields and calls the generation flow,
which resolves the tokens, fills the template, delivers the finished .docx back into the originating
conversation, and offers DocuSign. That generation seam is
:func:`app.bot.flows.generate_completion.run_generation`.
"""

from __future__ import annotations

from .generate_completion import (
    CompletionResult,
    run_generation,
    send_docusign_button_value,
)

__all__ = [
    "CompletionResult",
    "run_generation",
    "send_docusign_button_value",
]
