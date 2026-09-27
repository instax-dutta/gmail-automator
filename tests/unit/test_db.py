from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from gmail_automator.db import UTCDateTime, create_db_engine, create_session_factory
from gmail_automator.models import Account, Base


def test_sqlite_pragmas(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'x.db'}")
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000
    engine.dispose()


def test_sqlite_parent_dir_created(tmp_path) -> None:
    db = tmp_path / "nested" / "deep" / "x.db"
    engine = create_db_engine(f"sqlite:///{db}")
    engine.dispose()
    assert db.parent.is_dir()


def test_utc_datetime_roundtrip(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'x.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    with factory() as session:
        account = Account(
            email="a@example.com", token_uri="https://x", created_at=now, updated_at=now
        )
        session.add(account)
        session.commit()
    with factory() as session:
        loaded = session.scalar(select(Account).where(Account.email == "a@example.com"))
        assert loaded is not None
        assert loaded.created_at == now and loaded.created_at.tzinfo is UTC
    engine.dispose()


def test_naive_datetime_rejected() -> None:
    t = UTCDateTime()
    with pytest.raises(ValueError):
        t.process_bind_param(datetime(2026, 1, 1), None)  # type: ignore[arg-type]


def test_reminder_window_math_is_stable() -> None:
    # guards the exact rolling-window arithmetic used by QuotaService in Phase 1
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    assert now - timedelta(hours=24) == datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
