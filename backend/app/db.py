"""Database engine, session, and schema bootstrap (SQLite via SQLAlchemy 2.0).

SQLite is intentionally simple: a single file under DATA_DIR, no server process.
Documents themselves live on the filesystem; only metadata/records live here.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

# One shared semi-structured column type: native ``jsonb`` on Postgres (queryable, GIN-indexable),
# plain ``JSON`` (TEXT affinity) on SQLite dev/tests. Use this instead of ``Text`` + manual
# ``json.dumps/loads`` so the value round-trips as a native Python dict/list on both dialects.
JSON_VARIANT = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _make_engine():
    url = settings.database_url
    connect_args: dict = {}
    if url.startswith("sqlite"):
        # Allow use across threads (FastAPI threadpool + background tasks).
        connect_args = {"check_same_thread": False}
        # Ensure the parent directory exists for file-based SQLite URLs.
        if url.startswith("sqlite:///"):
            db_path = Path(url.replace("sqlite:///", "", 1))
            try:
                db_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                # Import-time failure, so a bare PermissionError here is a traceback with no
                # explanation during boot. Say what is actually wrong: in a container the app user
                # usually cannot write the image's working directory (see the Dockerfile's
                # /app/data), and the real fix is nearly always to point DATABASE_URL at Postgres.
                raise RuntimeError(
                    f"cannot create the SQLite directory {db_path.parent!s} ({exc}). "
                    "Set DATABASE_URL to a Postgres URL, or make that path writable by the "
                    "process user."
                ) from exc
    # Headroom for the FastAPI threadpool + multiple concurrent review workers
    # (the review-concurrency semaphore is the real bound; this avoids transient
    # pool-timeout errors during a burst of uploads). Recycle stale connections.
    engine = create_engine(
        url,
        connect_args=connect_args,
        future=True,
        pool_size=15,
        max_overflow=15,
        pool_timeout=30,
        pool_recycle=1800,
    )

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _):  # pragma: no cover - trivial
            # NOTE: we deliberately do NOT apply the "pysqlite SAVEPOINT" recipe (isolation_level=None
            # + an explicit BEGIN). That recipe makes a SELECT open a held read snapshot for the whole
            # transaction, so a later write on the same session (read-then-write — exactly the shape of
            # routes_clm.launch_review: _get_contract SELECT, long engine call, then flush) raises an
            # uncatchable, busy_timeout-immune "database is locked" whenever another connection commits
            # in the window. Default pysqlite begins a transaction only at the first DML, so a write
            # after a read starts fresh against current data. Code that needs atomic retry uses an
            # OUTER-transaction rollback rather than a savepoint (see routes_clm.add_document).
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            # Wait (don't error) when another thread holds the write lock — relevant now
            # that reviews run in the threadpool and may write concurrently.
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    return engine


engine = _make_engine()
SessionLocal = sessionmaker(
    bind=engine, autoflush=False, expire_on_commit=False, class_=Session
)


def init_db() -> None:
    """Create any missing tables (idempotent). Import models first so they register on
    ``Base.metadata``.

    Alembic is the SOLE source of truth for schema CHANGES: deploy runs ``python -m app.db_migrate``
    -> ``alembic upgrade head`` before the app starts, and every column/index is declared on the ORM
    models (so a fresh ``create_all`` is schema-equivalent to ``alembic upgrade head``). ``create_all``
    here is the idempotent fresh-DB/test safety net only; it never ALTERs an existing table.
    """
    from . import models  # noqa: F401  (populates Base.metadata)

    Base.metadata.create_all(bind=engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
