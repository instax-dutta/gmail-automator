from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from gmail_automator import models  # noqa: F401 - registers models on Base.metadata
from gmail_automator.db import Base, create_db_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # create_db_engine (not engine_from_config) so SQLite gets the same parent-directory
    # creation and WAL/busy_timeout/foreign_keys pragmas the application uses.
    connectable = create_db_engine(config.get_main_option("sqlalchemy.url"))
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
