from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import DateTime, Engine, TypeDecorator, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class UTCDateTime(TypeDecorator[datetime]):
    """Stores naive UTC in the DB, always returns timezone-aware UTC in Python."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime rejected: all datetimes must be timezone-aware UTC")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    pass


def _ensure_sqlite_parent(database_url: str) -> None:
    if not database_url.startswith("sqlite"):
        return
    _, _, tail = database_url.partition(":///")
    if not tail or tail == ":memory:":
        return
    Path(tail).parent.mkdir(parents=True, exist_ok=True)


def create_db_engine(database_url: str) -> Engine:
    _ensure_sqlite_parent(database_url)
    connect_args: dict[str, Any] = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(database_url, connect_args=connect_args, future=True)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def run_migrations(database_url: str, ini_path: Path) -> None:
    if not Path(ini_path).is_file():
        raise FileNotFoundError(
            f"alembic.ini not found at {ini_path}; set GMAIL_AUTOMATOR_ALEMBIC_INI to its location"
        )
    _ensure_sqlite_parent(database_url)
    config = Config(str(ini_path))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
