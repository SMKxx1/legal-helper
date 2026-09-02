"""Alembic environment — wired to the app's own settings + metadata.

The migration target URL comes from ``app.config.settings.database_url`` (SQLite in dev,
Postgres in prod), NOT from alembic.ini, so migrations always hit the same DB the app uses.
``Base.metadata`` is the autogenerate source of truth (importing ``app.models`` registers
every table). ``render_as_batch`` is enabled on SQLite so ALTER-heavy migrations (e.g. the
doc_sha256 UNIQUE relaxation in P0-2) work via the copy-and-rename table rebuild SQLite needs.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app import models  # noqa: F401 — import populates Base.metadata with every table
from app.auth import (
    models as _auth_models,  # noqa: F401 — registers the identity/org tables
)
from app.config import settings
from app.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# App settings own the URL (not alembic.ini), so `alembic upgrade head` and the running app
# always agree on which database they are talking to.
DB_URL = settings.database_url
config.set_main_option("sqlalchemy.url", DB_URL)

target_metadata = Base.metadata


def _is_sqlite() -> bool:
    return DB_URL.startswith("sqlite")


def run_migrations_offline() -> None:
    """Emit SQL without a live DB connection (`alembic upgrade --sql`)."""
    context.configure(
        url=DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite(),
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection (NullPool — migrations are short-lived)."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = DB_URL
    connect_args = {"check_same_thread": False} if _is_sqlite() else {}
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=_is_sqlite(),
            compare_type=True,
            # Wrap EACH migration in its own transaction so a mid-run failure rolls that migration
            # back cleanly instead of leaving a half-applied schema (esp. on SQLite, whose DDL is
            # otherwise auto-committed statement-by-statement).
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
