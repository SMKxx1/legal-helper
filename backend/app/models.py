"""ORM models — placeholder.

The legacy engine's ``models.py``/``models_v2.py``/``models_bot.py`` (contracts, templates, tokens,
signing, channels, bot inbox …) were removed in the Phase 0 strip: none of that is part of Legal
Helper. Phase 1 writes the real tables here per the plan's data model (``User``, ``OpenRouterKey``,
``Review``, ``LlmCall``). This module exists now only so ``app.db.init_db`` has something importable
that registers on ``Base.metadata`` without error.
"""

from __future__ import annotations
