import asyncio
import os
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

# psycopg's async mode can't run on Windows' default Proactor loop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Settings are read at import time, so point them at the test database before importing the app.
_HOST_PORT = os.environ.get("POSTGRES_HOST_PORT", "5433")
_TEST_DB = f"127.0.0.1:{_HOST_PORT}/aegisgrid_test"
os.environ["AEGIS_ENV"] = "test"
os.environ["AEGIS_DATABASE_URL"] = f"postgresql+psycopg://aegis_api:aegis_api@{_TEST_DB}"
os.environ["AEGIS_MIGRATOR_DATABASE_URL"] = (
    f"postgresql+psycopg://aegis_migrator:aegis_migrator@{_TEST_DB}"
)
# Tests wipe rate-limit keys: keep them off the dev app's Redis database (0).
_REDIS_PORT = os.environ.get("REDIS_HOST_PORT", "6379")
os.environ["AEGIS_REDIS_URL"] = f"redis://127.0.0.1:{_REDIS_PORT}/15"

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from redis.asyncio import Redis  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core import db  # noqa: E402
from app.core.cache import redis as redis_client  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """Bring aegisgrid_test to head once per session, as aegis_migrator."""
    command.upgrade(Config(str(Path(__file__).parent.parent / "alembic.ini")), "head")


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """aegis_api session inside an outer transaction that is rolled back after each test.

    Code under test may commit or roll back freely: those become SAVEPOINT operations.
    """
    async with db.engine.connect() as conn:
        outer = await conn.begin()
        session = AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield session
        finally:
            await session.close()
            await outer.rollback()


@pytest.fixture
def app(db_session: AsyncSession) -> Iterator[FastAPI]:
    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    fastapi_app.dependency_overrides[db.get_session] = _session
    yield fastapi_app
    fastapi_app.dependency_overrides.clear()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def redis() -> Redis:
    return redis_client
