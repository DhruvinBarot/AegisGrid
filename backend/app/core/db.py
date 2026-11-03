from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, MetaData
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core import tenancy  # noqa: F401  (registers the tenant guard session hooks)
from app.core.config import settings

# Always aegis_api. Alembic reads the migrator URL itself; the app never has it.
engine = create_async_engine(
    settings.database_url.get_secret_value(),
    pool_pre_ping=True,
    connect_args={"connect_timeout": 2},
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    # Stable constraint names keep migrations and autogenerate diffs deterministic.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
    type_annotation_map = {datetime: DateTime(timezone=True), dict[str, Any]: JSONB}


async def get_session() -> AsyncIterator[AsyncSession]:
    """Request-scoped session. Callers own the transaction; tests override this dependency."""
    async with SessionLocal() as session:
        yield session
