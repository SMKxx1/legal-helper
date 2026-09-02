"""Unit test for the prompt-release provenance id (#6)."""

from __future__ import annotations

import re

from app.engine.prompt_release import prompt_release_id


def test_prompt_release_id_is_16_hex_and_stable():
    rid = prompt_release_id()
    assert re.fullmatch(r"[0-9a-f]{16}", rid), rid
    # Stable across calls (lru-cached, deterministic over the module-level prompt constants).
    assert prompt_release_id() == rid
