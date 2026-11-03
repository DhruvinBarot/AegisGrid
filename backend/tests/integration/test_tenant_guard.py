"""Structural tenant guard (ADR-0003), exercised against rows created through the API."""

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import decode_id
from app.core.tenancy import (
    OrgContext,
    TenantContextError,
    get_in_org,
    org_scope,
    system_scope,
)
from app.modules.environments.models import Environment
from app.modules.organizations.models import Membership
from tests.integration.helpers import Api, Populated, populate


def ctx_for(org: str) -> OrgContext:
    return OrgContext(
        org_id=decode_id("org", org),
        principal_id=uuid.uuid4(),
        role="owner",
        permissions=frozenset(),
    )


@pytest.fixture
async def two(api: Api) -> tuple[Populated, Populated]:
    return await populate(api, "a"), await populate(api, "b")


async def test_query_without_context_raises(
    db_session: AsyncSession, two: tuple[Populated, Populated]
) -> None:
    for model in (Environment, Membership):
        with pytest.raises(TenantContextError):
            await db_session.execute(select(model))
    with pytest.raises(TenantContextError):
        await db_session.get(Environment, decode_id("env", two[0].ids["env"]))


async def test_query_with_context_returns_only_that_orgs_rows(
    db_session: AsyncSession, two: tuple[Populated, Populated]
) -> None:
    a, b = two
    with system_scope():
        every = (await db_session.scalars(select(Environment))).all()
    # both orgs have environments, so the per-org filter below is doing real work
    assert {e.organization_id for e in every} == {decode_id("org", p.org) for p in two}
    for mine, theirs in ((a, b), (b, a)):
        with org_scope(ctx_for(mine.org)):
            envs = (await db_session.scalars(select(Environment))).all()
            assert envs and {e.organization_id for e in envs} == {decode_id("org", mine.org)}
            members = (await db_session.scalars(select(Membership))).all()
            assert {m.organization_id for m in members} == {decode_id("org", mine.org)}
            # asking for the other org's row by primary key finds nothing
            assert (
                await get_in_org(db_session, Environment, decode_id("env", theirs.ids["env"]))
                is None
            )
            other = select(Environment).where(
                Environment.organization_id == decode_id("org", theirs.org)
            )
            assert (await db_session.scalars(other)).all() == []


async def test_core_sql_on_tenant_table_is_refused_even_with_context(
    db_session: AsyncSession, two: tuple[Populated, Populated]
) -> None:
    with org_scope(ctx_for(two[0].org)):
        with pytest.raises(TenantContextError):
            await db_session.execute(select(Environment.__table__))


async def test_writing_another_orgs_row_is_refused(
    db_session: AsyncSession, two: tuple[Populated, Populated]
) -> None:
    a, b = two
    with org_scope(ctx_for(a.org)):
        db_session.add(
            Environment(
                organization_id=decode_id("org", b.org), name="smuggled", kind="development"
            )
        )
        with pytest.raises(TenantContextError):
            await db_session.flush()
    db_session.expunge_all()
    with system_scope():
        rows = (
            await db_session.execute(select(func.count()).where(Environment.name == "smuggled"))
        ).scalar_one()
    assert rows == 0


async def test_system_scope_sees_every_org(
    db_session: AsyncSession, two: tuple[Populated, Populated]
) -> None:
    with system_scope():
        orgs = {e.organization_id for e in (await db_session.scalars(select(Environment))).all()}
    assert orgs == {decode_id("org", p.org) for p in two}
