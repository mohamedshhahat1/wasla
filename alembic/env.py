"""Alembic environment.

The database URL comes from application settings rather than alembic.ini, and
the metadata comes from app.db.models, so autogenerate always compares against
the models the application actually uses.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

settings = get_settings()

# A caller that drives Alembic as a library - the integration suite building a
# migration-built schema - passes the database it means explicitly. Nothing
# else sets this, so a command-line `alembic upgrade head` still takes the URL
# from application settings and there remains one source of truth for it.
#
# Passed as an attribute rather than read from the environment on purpose: the
# environment is where the developer's own `DATABASE_URL` lives, and a test
# fixture that could not out-argue it would migrate their working database.
database_url = config.attributes.get("wasla_database_url") or settings.database_url

# Escape % so ConfigParser interpolation cannot mangle credentials.
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
