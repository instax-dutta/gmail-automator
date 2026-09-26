from pathlib import Path

from sqlalchemy import inspect, text

from fmaiily.db import create_db_engine, run_migrations

INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def test_upgrade_head_creates_schema(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    run_migrations(url, INI)
    engine = create_db_engine(url)
    tables = set(inspect(engine).get_table_names())
    assert {"accounts", "send_jobs", "send_events", "api_keys", "oauth_states"} <= tables
    with engine.connect() as conn:
        assert conn.execute(text("select version_num from alembic_version")).scalar() == "0001"
    engine.dispose()
