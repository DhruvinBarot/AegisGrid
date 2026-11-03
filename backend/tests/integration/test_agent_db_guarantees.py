"""Database-level guarantees for agents (ADR-0003, ADR-0004, ADR-0007): they hold even for code
that bypasses the API and the tenancy guard."""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.db import SessionLocal
from app.core.tenancy import OrgContext, org_scope
from app.modules.agents import service
from app.modules.agents.schemas import PublishIn
from app.modules.organizations.deps import AuditMeta
from tests.integration.helpers import SPEC, Api, populate

INSUFFICIENT_PRIVILEGE = "42501"
FOREIGN_KEY_VIOLATION = "23503"
UNIQUE_VIOLATION = "23505"


def sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


async def seed(conn: AsyncConnection, tag: str) -> dict[str, uuid.UUID]:
    """One org with an agent, a version, two environments and a deployment, inserted directly."""
    ids = {k: uuid.uuid4() for k in ("org", "prn", "agent", "agent2", "ver", "env", "env2", "dep")}
    for sql in (
        "INSERT INTO organizations (id, slug, name) VALUES (:org, :tag, 'Seed')",
        "INSERT INTO principals (id, kind) VALUES (:prn, 'service')",
        "INSERT INTO agents (id, organization_id, slug, name, created_by) VALUES "
        "(:agent, :org, 'a', 'A', :prn), (:agent2, :org, 'b', 'B', :prn)",
        "INSERT INTO agent_versions (id, organization_id, agent_id, version_no, spec, "
        "spec_schema_version, content_hash, created_by) VALUES "
        "(:ver, :org, :agent, 1, '{}', 1, 'sha256:' || repeat('0', 64), :prn)",
        "INSERT INTO environments (id, organization_id, name, kind) VALUES "
        "(:env, :org, 'dev', 'development'), (:env2, :org, 'stg', 'staging')",
        "INSERT INTO agent_deployments (id, organization_id, agent_id, environment_id, "
        "agent_version_id, reason, deployed_by) "
        "VALUES (:dep, :org, :agent, :env, :ver, 'deploy', :prn)",
    ):
        await conn.execute(text(sql), {**ids, "tag": f"seed-{tag}"})
    return ids


@pytest.fixture
async def migrator() -> AsyncIterator[AsyncConnection]:
    """aegis_migrator (the table owner, so grants don't apply). Everything is rolled back."""
    engine = create_async_engine(os.environ["AEGIS_MIGRATOR_DATABASE_URL"], poolclass=NullPool)
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT current_user"))).scalar_one() == "aegis_migrator"
        yield conn
        await conn.rollback()
    await engine.dispose()


async def test_api_role_cannot_rewrite_history(api: Api, db_session: AsyncSession) -> None:
    await populate(api, "imm")  # a published version and a deployment
    for sql in (
        "UPDATE agent_versions SET notes = 'tampered'",
        "UPDATE agent_versions SET spec = '{}'::jsonb",
        "DELETE FROM agent_versions",
        "TRUNCATE agent_versions CASCADE",
        "UPDATE agent_deployments SET reason = 'rollback'",
        "DELETE FROM agent_deployments",
        "DELETE FROM agents",
        "DELETE FROM agent_environment_state",
    ):
        with pytest.raises(DBAPIError) as e:
            async with db_session.begin_nested():
                await db_session.execute(text(sql))
        assert sqlstate(e.value) == INSUFFICIENT_PRIVILEGE, sql


@pytest.mark.parametrize("table", ["agent_versions", "agent_deployments"])
@pytest.mark.parametrize(
    "op", ["UPDATE {t} SET notes = 'x'", "DELETE FROM {t}", "TRUNCATE {t} CASCADE"]
)
async def test_even_the_table_owner_cannot_mutate_history(
    migrator: AsyncConnection, table: str, op: str
) -> None:
    await seed(migrator, uuid.uuid4().hex[:8])
    with pytest.raises(DBAPIError) as e:
        async with migrator.begin_nested():
            await migrator.execute(text(op.format(t=table)))
    assert sqlstate(e.value) == INSUFFICIENT_PRIVILEGE
    assert f"{table} is immutable" in str(e.value)


@pytest.mark.parametrize(
    ("label", "sql"),
    [
        (
            "version of another org",
            "INSERT INTO agent_deployments (id, organization_id, agent_id, environment_id, "
            "agent_version_id, reason, deployed_by) VALUES "
            "(gen_random_uuid(), :b_org, :a_agent, :b_env, :a_ver, 'deploy', :b_prn)",
        ),
        (
            "environment of another org",
            "INSERT INTO agent_deployments (id, organization_id, agent_id, environment_id, "
            "agent_version_id, reason, deployed_by) VALUES "
            "(gen_random_uuid(), :a_org, :a_agent, :b_env, :a_ver, 'deploy', :a_prn)",
        ),
        (
            "version of another agent",
            "INSERT INTO agent_deployments (id, organization_id, agent_id, environment_id, "
            "agent_version_id, reason, deployed_by) VALUES "
            "(gen_random_uuid(), :a_org, :a_agent2, :a_env, :a_ver, 'deploy', :a_prn)",
        ),
        (
            "version under another org's agent",
            "INSERT INTO agent_versions (id, organization_id, agent_id, version_no, spec, "
            "spec_schema_version, content_hash, created_by) VALUES (gen_random_uuid(), :b_org, "
            ":a_agent, 9, '{}', 1, 'sha256:' || repeat('0', 64), :b_prn)",
        ),
        (
            "pointer to a deployment of another environment",
            "INSERT INTO agent_environment_state (id, organization_id, agent_id, environment_id, "
            "active_deployment_id) VALUES (gen_random_uuid(), :a_org, :a_agent, :a_env2, :a_dep)",
        ),
    ],
)
async def test_composite_foreign_keys_reject_mismatched_references(
    migrator: AsyncConnection, label: str, sql: str
) -> None:
    a, b = await seed(migrator, uuid.uuid4().hex[:8]), await seed(migrator, uuid.uuid4().hex[:8])
    params = {f"a_{k}": v for k, v in a.items()} | {f"b_{k}": v for k, v in b.items()}
    with pytest.raises(IntegrityError) as e:
        async with migrator.begin_nested():
            await migrator.execute(text(sql), params)
    assert sqlstate(e.value) == FOREIGN_KEY_VIOLATION, label


async def test_version_numbers_are_unique_per_agent(migrator: AsyncConnection) -> None:
    ids = await seed(migrator, uuid.uuid4().hex[:8])  # already holds version 1
    with pytest.raises(IntegrityError) as e:
        async with migrator.begin_nested():
            await migrator.execute(
                text(
                    "INSERT INTO agent_versions (id, organization_id, agent_id, version_no, spec, "
                    "spec_schema_version, content_hash, created_by) VALUES (gen_random_uuid(), "
                    ":org, :agent, 1, '{}', 1, 'sha256:' || repeat('0', 64), :prn)"
                ),
                ids,
            )
    assert sqlstate(e.value) == UNIQUE_VIOLATION


@pytest.fixture
async def committed_agent() -> AsyncIterator[dict[str, uuid.UUID]]:
    """An org, principal and agent with a valid draft, committed so a second connection sees
    them. Seeded as the table owner (so no audit rows) and deleted afterwards; none of these
    tables is append-only, so the test database is left exactly as it was."""
    engine = create_async_engine(os.environ["AEGIS_MIGRATOR_DATABASE_URL"], poolclass=NullPool)
    ids = {k: uuid.uuid4() for k in ("org", "prn", "agent")}
    async with engine.begin() as conn:
        for sql in (
            "INSERT INTO organizations (id, slug, name) VALUES (:org, :slug, 'Race')",
            "INSERT INTO principals (id, kind) VALUES (:prn, 'service')",
            "INSERT INTO agents (id, organization_id, slug, name, created_by, draft_spec, "
            "draft_rev) VALUES (:agent, :org, 'race', 'Race', :prn, CAST(:spec AS jsonb), 1)",
        ):
            await conn.execute(
                text(sql), {**ids, "slug": f"race-{ids['org'].hex}", "spec": json.dumps(SPEC)}
            )
    try:
        yield ids
    finally:
        async with engine.begin() as conn:
            for table, key in (
                ("agents", "agent"),
                ("principals", "prn"),
                ("organizations", "org"),
            ):
                await conn.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": ids[key]})
        await engine.dispose()


async def test_publishes_of_one_agent_serialize_on_its_row_lock(
    committed_agent: dict[str, uuid.UUID],
) -> None:
    """Two real connections. While the first publish holds the agent row lock, the second waits;
    it computes its version number only after the lock is released. Both transactions are rolled
    back, so no (undeletable) version is ever committed."""
    ids = committed_agent
    ctx = OrgContext(
        org_id=ids["org"], principal_id=ids["prn"], role="owner", permissions=frozenset()
    )
    meta: AuditMeta = {"ip": None, "user_agent": None, "request_id": None}
    with org_scope(ctx):
        async with SessionLocal() as first, SessionLocal() as second:
            v = await service.publish(first, ctx, ids["agent"], PublishIn(), meta)
            assert v.version_no == 1
            waiting = asyncio.create_task(
                service.publish(second, ctx, ids["agent"], PublishIn(), meta)
            )
            await asyncio.sleep(0.5)
            assert not waiting.done(), "a second publish ran while the first held the lock"
            await first.rollback()
            again = await asyncio.wait_for(waiting, timeout=10)
            assert again.version_no == 1  # numbered from committed state, after the lock
            await second.rollback()
