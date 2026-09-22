"""Database engine and session helpers.

The engine is created lazily so importing this module never requires Postgres
to be running — useful for tests and for the local webhook simulator.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, text
from sqlmodel import Session as DBSession
from sqlmodel import SQLModel, create_engine

import models  # noqa: F401  — registers tables on SQLModel.metadata


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url = os.environ["DATABASE_URL"]
    # pool_pre_ping avoids stale connections after the Postgres container restarts
    return create_engine(url, echo=False, pool_pre_ping=True)


# Columns added after their table already existed. create_all() only creates
# missing tables, never columns, and there is no Alembic yet (CLAUDE.md), so
# each addition is an idempotent ALTER that keeps existing rows.
_ADDED_COLUMNS = (
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS messages JSONB",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS profile JSONB",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS pending_address JSONB",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS shipping JSONB",
)


def init_db() -> None:
    """Create any missing tables and columns. Safe to call repeatedly."""
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        for statement in _ADDED_COLUMNS:
            connection.execute(text(statement))


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
