"""Seed synthetic demo data (``make seed`` / ``SEED_DEMO_DATA=true`` on boot).

Phase 0 stub: prints what it *will* do so ``make seed`` works today without a broken target. Phase 1
adds the synthetic users (so you can sign in immediately); Phase 3 adds the ~140 synthetic reviews +
``llm_calls`` rows the Usage tab reads (see plan §4.6).
"""

from __future__ import annotations

from .telemetry import get_logger

log = get_logger("legal_helper.seed_demo")


def run() -> None:
    log.info(
        "seed_demo.not_yet_implemented",
        note="Phase 1 seeds synthetic users; Phase 3 adds reviews + llm_calls.",
    )


if __name__ == "__main__":
    run()
