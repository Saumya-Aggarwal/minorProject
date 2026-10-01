"""Database engine and session helpers.

The engine is created lazily so importing this module never requires Postgres
to be running — useful for tests and for the local webhook simulator.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text
from sqlmodel import Session as DBSession
from sqlmodel import create_engine

import models  # noqa: F401  — registers tables on SQLModel.metadata

BACKEND = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url = os.environ["DATABASE_URL"]
    # pool_pre_ping avoids stale connections after the Postgres container restarts
    return create_engine(url, echo=False, pool_pre_ping=True)


# The schema is managed by Alembic (backend/migrations). A database created
# before Alembic was introduced (1 Oct 2026) has the tables but no
# alembic_version row: it gets the column additions it may lack, which were
# idempotent ALTERs run on every start until then, and is stamped as the
# baseline it then matches.
_PRE_ALEMBIC_COLUMNS = (
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS messages JSONB",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS profile JSONB",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS pending_address JSONB",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS shipping JSONB",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS reorder JSONB",
)
_BASELINE = "0001"
# Any constant: serialises migrations when the app and a test run start together
_MIGRATION_LOCK = 724_001


def _alembic_config(connection) -> Config:
    config = Config(str(BACKEND / "alembic.ini"))
    config.attributes["connection"] = connection
    config.attributes["configure_logger"] = False
    return config


def init_db() -> None:
    """Bring the schema up to date (alembic upgrade head) and make sure every
    catalogue is loaded and embedded. Safe to call repeatedly."""
    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _MIGRATION_LOCK})
        config = _alembic_config(connection)
        tables = set(inspect(connection).get_table_names())
        if "users" in tables and "alembic_version" not in tables:
            for statement in _PRE_ALEMBIC_COLUMNS:
                connection.execute(text(statement))
            command.stamp(config, _BASELINE)
            print(f"[db] existing schema adopted as Alembic revision {_BASELINE}")
        command.upgrade(config, "head")

    # Data, not schema: load a feed store that has no products yet (the demo
    # store on a fresh database) and re-embed vectors from another model
    import catalog_sync

    catalog_sync.ensure_catalogues()


@contextmanager
def session_scope() -> Iterator[DBSession]:
    """Transaction boundary: commits on success, rolls back on error."""
    session = DBSession(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
