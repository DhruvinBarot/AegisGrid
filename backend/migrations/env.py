import asyncio
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.db import Base

# Importing the models registers their tables on Base.metadata.
from app.modules.agents import models as _agents  # noqa: F401
from app.modules.audit import models as _audit  # noqa: F401
from app.modules.environments import models as _environments  # noqa: F401
from app.modules.identity import models as _identity  # noqa: F401
from app.modules.organizations import models as _organizations  # noqa: F401

config = context.config
_DEV_MIGRATOR_URL = "postgresql+psycopg://aegis_migrator:aegis_migrator@127.0.0.1:5433/aegisgrid"


def _migrator_url() -> str:
    # Migrations run as aegis_migrator and nothing else may (ADR-0004). The URL comes straight from
    # the environment, never from app settings, so the running API never holds these credentials.
    url = os.environ.get("AEGIS_MIGRATOR_DATABASE_URL")
    if url:
        return url
    if os.environ.get("AEGIS_ENV") == "development":
        return _DEV_MIGRATOR_URL
    raise RuntimeError("AEGIS_MIGRATOR_DATABASE_URL is required")


# "%" must be doubled: Alembic runs ini values through ConfigParser interpolation.
config.set_main_option("sqlalchemy.url", _migrator_url().replace("%", "%%"))

if config.config_file_name is not None:
    # Keep app loggers alive: tests run migrations in-process before exercising app logging.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
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
    # psycopg's async mode can't run on Windows' default Proactor loop.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
