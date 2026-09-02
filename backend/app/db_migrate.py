"""Deploy-time DB migration entrypoint — run BEFORE the app starts (Dockerfile CMD).

One command, `alembic upgrade head`: a fresh database gets the baseline migration (creates every
table); an already-migrated one gets whatever is pending. Alembic is the sole source of truth for
schema CHANGES — `init_db()`'s `create_all` (used by dev/tests) is only ever a fresh-DB shortcut,
never a second way to alter an existing table.

Run with ``python -m app.db_migrate`` from the backend dir (where alembic.ini lives).
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic.config import Config

from alembic import command

logger = logging.getLogger("legal_helper.db_migrate")


def _alembic_config() -> Config:
    # alembic.ini sits next to the backend package root; resolve it so this works regardless of cwd.
    ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    return Config(str(ini))


def run() -> None:
    logger.info("alembic upgrade head")
    command.upgrade(_alembic_config(), "head")
    logger.info("migrations up to date")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s"
    )
    run()
