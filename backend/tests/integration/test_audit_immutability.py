"""audit_log is append-only for everyone, and the API role cannot change the schema (ADR-0004)."""

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.helpers import Api

INSUFFICIENT_PRIVILEGE = "42501"


def sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


async def test_api_role_can_insert_and_select_but_not_update_or_delete(
    api: Api, db_session: AsyncSession
) -> None:
    await api.actor("audited@example.com")  # writes an auth.register row
    n = (await db_session.execute(text("SELECT count(*) FROM audit_log"))).scalar_one()
    assert n >= 1
    for sql in (
        "UPDATE audit_log SET action = 'tampered'",
        "UPDATE audit_log SET outcome = 'success' WHERE false",
        "DELETE FROM audit_log",
        "DELETE FROM audit_log WHERE false",
        "TRUNCATE audit_log",
    ):
        with pytest.raises(DBAPIError) as e:
            async with db_session.begin_nested():
                await db_session.execute(text(sql))
        assert sqlstate(e.value) == INSUFFICIENT_PRIVILEGE, sql
    assert (await db_session.execute(text("SELECT count(*) FROM audit_log"))).scalar_one() == n


async def test_api_role_runs_as_aegis_api_without_ddl_rights(db_session: AsyncSession) -> None:
    assert (await db_session.execute(text("SELECT current_user"))).scalar_one() == "aegis_api"
    for sql in (
        "CREATE TABLE evil (id int)",
        "CREATE TABLE public.evil (id int)",
        "ALTER TABLE audit_log DISABLE TRIGGER ALL",
        "DROP TABLE audit_log",
    ):
        with pytest.raises(DBAPIError) as e:
            async with db_session.begin_nested():
                await db_session.execute(text(sql))
        assert sqlstate(e.value) == INSUFFICIENT_PRIVILEGE or "must be owner" in str(e.value), sql
    has_create = (
        await db_session.execute(
            text("SELECT has_schema_privilege('aegis_api', 'public', 'CREATE')")
        )
    ).scalar_one()
    assert has_create is False


@pytest.fixture
async def migrator() -> AsyncIterator[AsyncConnection]:
    """A separate connection as aegis_migrator (the table owner). Everything is rolled back."""
    engine = create_async_engine(os.environ["AEGIS_MIGRATOR_DATABASE_URL"], poolclass=NullPool)
    async with engine.connect() as conn:
        user = (await conn.execute(text("SELECT current_user"))).scalar_one()
        assert user == "aegis_migrator"
        await conn.execute(
            text(
                "INSERT INTO audit_log (id, actor_type, action) "
                "VALUES (gen_random_uuid(), 'system', 'test.immutability')"
            )
        )
        yield conn
        await conn.rollback()
    await engine.dispose()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE audit_log SET action = 'tampered' WHERE action = 'test.immutability'",
        "DELETE FROM audit_log WHERE action = 'test.immutability'",
        "TRUNCATE audit_log",
    ],
    ids=["update", "delete", "truncate"],
)
async def test_even_the_table_owner_cannot_mutate_audit_rows(
    migrator: AsyncConnection, sql: str
) -> None:
    with pytest.raises(DBAPIError) as e:
        await migrator.execute(text(sql))
    # the trigger raises insufficient_privilege; the message tells it apart from a missing GRANT
    assert sqlstate(e.value) == INSUFFICIENT_PRIVILEGE
    assert "append-only" in str(e.value)


async def test_trigger_blocks_update_on_every_row_even_via_cte(migrator: AsyncConnection) -> None:
    with pytest.raises(DBAPIError) as e:
        await migrator.execute(
            text(
                "WITH x AS (SELECT id FROM audit_log LIMIT 1) "
                "UPDATE audit_log SET ip = '1.1.1.1' WHERE id IN (SELECT id FROM x)"
            )
        )
    assert "append-only" in str(e.value)
