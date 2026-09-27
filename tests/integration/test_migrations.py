from pathlib import Path

from sqlalchemy import inspect, text

from gmail_automator.db import create_db_engine, run_migrations

INI = Path(__file__).resolve().parents[2] / "alembic.ini"
VERSIONS = INI.parent / "migrations" / "versions"


def _head_revision() -> str:
    """The newest revision on disk, so the assertion tracks new migrations automatically."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(Config(str(INI))).get_current_head()


def test_upgrade_head_creates_schema(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    run_migrations(url, INI)
    engine = create_db_engine(url)
    tables = set(inspect(engine).get_table_names())
    assert {"accounts", "send_jobs", "send_events", "api_keys", "oauth_states"} <= tables
    with engine.connect() as conn:
        head = conn.execute(text("select version_num from alembic_version")).scalar()
    engine.dispose()
    assert _head_revision() == head
