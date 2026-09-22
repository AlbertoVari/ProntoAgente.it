"""Synchronous SQLAlchemy setup."""

import sqlite3
from collections.abc import Iterator
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from prontoagente.config import get_settings


class Base(DeclarativeBase):
    """Declarative model base."""


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
    """Enable referential integrity on every synchronous SQLite connection."""

    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def build_engine(database_url: str) -> Engine:
    """Build an engine with the SQLite thread option only when needed."""

    connect_args: dict[str, bool] = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)


engine = build_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency that always closes and rolls back failed units of work."""

    with SessionLocal() as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise
