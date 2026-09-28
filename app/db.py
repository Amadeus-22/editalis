"""Engine/session factories and dialect-aware helpers."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base


def make_engine(url: str, **kwargs: Any) -> Engine:
    if url.startswith("sqlite"):
        kwargs.setdefault("connect_args", {"check_same_thread": False})
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


@lru_cache
def get_engine(url: str) -> Engine:
    return make_engine(url)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def alembic_config(url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False
    return config


def upgrade_db(url: str) -> None:
    """Apply all pending Alembic migrations. Used by the API and the worker."""
    command.upgrade(alembic_config(url), "head")


def init_db(engine: Engine) -> None:
    """Create tables straight from the models. For tests and throwaway databases only."""
    Base.metadata.create_all(engine)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def insert_ignore(
    session: Session,
    model: type[Base],
    rows: Sequence[dict[str, Any]],
    conflict_columns: Sequence[str],
    chunk_size: int = 500,
) -> int:
    """Insert rows, silently skipping ones that violate the given unique key.

    Returns the number of rows actually inserted. This is what keeps every
    pipeline stage safe to re-run.
    """
    if not rows:
        return 0
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise NotImplementedError(f"insert_ignore does not support dialect {dialect!r}")

    inserted = 0
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start : start + chunk_size]
        stmt = (
            insert(model)
            .values(list(chunk))
            .on_conflict_do_nothing(index_elements=list(conflict_columns))
        )
        inserted += session.execute(stmt).rowcount or 0
    return inserted
