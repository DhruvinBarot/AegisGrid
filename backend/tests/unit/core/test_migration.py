import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine

from app.core import db
from app.core.ids import new_id
from app.modules.audit.service import list_for_org, record


@pytest.fixture
async def migrator() -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(os.environ["AEGIS_MIGRATOR_DATABASE_URL"])
    async with engine.connect() as conn:
        txn = await conn.begin()
        yield conn
        await txn.rollback()
    await engine.dispose()


async def test_audit_record_inserts_in_callers_transaction(db_session: AsyncSession) -> None:
    actor = new_id()
    await record(db_session, organization_id=None, actor_type="user", actor_id=actor, action="x.y")
    row = (
        await db_session.execute(
            text("SELECT action, outcome FROM audit_log WHERE actor_id = :a"), {"a": actor}
        )
    ).one()
    assert tuple(row) == ("x.y", "success")


async def test_trigger_rejects_update_delete_truncate(migrator: AsyncConnection) -> None:
    await migrator.execute(
        text("INSERT INTO audit_log (id, actor_type, action) VALUES (:i, 'system', 'a')"),
        {"i": new_id()},
    )
    statements = (
        "UPDATE audit_log SET action = 'b'",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
    )
    for stmt in statements:
        with pytest.raises(DBAPIError, match="append-only"):
            async with migrator.begin_nested():
                await migrator.execute(text(stmt))


async def test_api_role_cannot_mutate_audit_log() -> None:
    async with db.engine.connect() as conn:
        for stmt in ("UPDATE audit_log SET action = 'b'", "DELETE FROM audit_log"):
            with pytest.raises(ProgrammingError, match="permission denied"):
                await conn.execute(text(stmt))
            await conn.rollback()


async def test_api_role_has_no_ddl() -> None:
    async with db.engine.connect() as conn:
        who = await conn.scalar(text("SELECT current_user"))
        with pytest.raises(ProgrammingError, match="permission denied"):
            await conn.execute(text("CREATE TABLE sneaky (id int)"))
        await conn.rollback()
    assert who == "aegis_api"


async def test_api_role_cannot_delete_soft_delete_tables() -> None:
    async with db.engine.connect() as conn:
        for table in ("organizations", "users", "principals", "signing_keys"):
            with pytest.raises(ProgrammingError, match="permission denied"):
                await conn.execute(text(f"DELETE FROM {table}"))
            await conn.rollback()


async def test_audit_list_for_org_is_scoped_and_paginated(db_session: AsyncSession) -> None:
    org, other = new_id(), new_id()
    for i in range(3):
        await record(db_session, organization_id=org, actor_type="u", actor_id=None, action=f"a{i}")
    await record(db_session, organization_id=other, actor_type="u", actor_id=None, action="x")
    page1 = await list_for_org(db_session, org, limit=2)
    assert len(page1) == 2 and {r.organization_id for r in page1} == {org}
    last = page1[-1]
    page2 = await list_for_org(db_session, org, limit=2, before=(last.occurred_at, last.id))
    assert len(page2) == 1
    assert {r.id for r in page1}.isdisjoint(r.id for r in page2)
