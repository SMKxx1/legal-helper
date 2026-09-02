"""Deploy-time DB migration entrypoint — run BEFORE the app starts (Dockerfile CMD).

Handles all three states a database can be in when this ships, so a deploy never crash-loops:

  * EMPTY DB (fresh)                  -> `upgrade head` creates everything + records the version.
  * PRE-ALEMBIC DB (create_all built it, no ``alembic_version`` table) -> STAMP the baseline first
    (it already HAS the baseline schema; running the baseline's create_table would fail), then
    `upgrade head` applies anything past the baseline.
  * ALREADY-MIGRATED DB              -> `upgrade head` applies pending revisions only.

Run with ``python -m app.db_migrate`` from the backend dir (where alembic.ini lives).
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic.config import Config
from sqlalchemy import inspect

from alembic import command
from app import (
    models as _models,  # noqa: F401 — register engine tables on Base.metadata
)
from app.auth import models as _auth_models  # noqa: F401 — register identity tables
from app.db import Base, engine

logger = logging.getLogger("nda.db_migrate")

_BASELINE_REVISION = "0001_baseline"
#: Any of these existing means the schema predates Alembic (an old create_all-built DB, pre-PL-2).
_PRE_ALEMBIC_MARKERS = ("contracts", "app_settings", "engine_reviews", "reviews")


def _alembic_config() -> Config:
    # alembic.ini sits next to the backend package root; resolve it so this works regardless of cwd.
    ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    return Config(str(ini))


def run() -> None:
    tables = set(inspect(engine).get_table_names())
    cfg = _alembic_config()

    if "alembic_version" not in tables and any(
        t in tables for t in _PRE_ALEMBIC_MARKERS
    ):
        # A schema-bearing DB with no Alembic bookkeeping. Stamp it at the revision its schema
        # already satisfies, so `upgrade` never tries to re-create existing tables:
        #   * already has EVERY current table (built by a current create_all) -> stamp head;
        #   * the genuine pre-Alembic baseline (missing newer tables) -> stamp baseline and let
        #     `upgrade` fill the gap forward.
        metadata_tables = set(Base.metadata.tables.keys())
        target = "head" if metadata_tables.issubset(tables) else _BASELINE_REVISION
        logger.info("pre-Alembic database detected — stamping %s", target)
        command.stamp(cfg, target)

    logger.info("alembic upgrade head")
    command.upgrade(cfg, "head")
    logger.info("migrations up to date")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s"
    )
    run()
