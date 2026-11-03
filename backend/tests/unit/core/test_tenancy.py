import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Select, delete, exists, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id
from app.core.tenancy import (
    OrgContext,
    TenantContextError,
    bind_org,
    get_in_org,
    org_scope,
    system_scope,
)
from app.modules.environments.models import Environment
from app.modules.identity.models import Principal
from app.modules.organizations.models import Membership, Organization


def ctx_for(org_id: uuid.UUID) -> OrgContext:
    return OrgContext(
        org_id=org_id, principal_id=new_id(), role="owner", permissions=frozenset({"*"})
    )


async def seed(
    session: AsyncSession,
) -> tuple[Organization, Organization, Environment, Environment]:
    org_a = Organization(slug=f"a-{new_id().hex[:8]}", name="A")
    org_b = Organization(slug=f"b-{new_id().hex[:8]}", name="B")
    with system_scope():
        session.add_all([org_a, org_b])
        await session.flush()
        env_a = Environment(organization_id=org_a.id, name="dev", kind="development")
        env_b = Environment(organization_id=org_b.id, name="dev", kind="development")
        session.add_all([env_a, env_b])
        await session.flush()
    return org_a, org_b, env_a, env_b


def shapes(org_a: Organization, org_b: Organization) -> dict[str, Select]:
    """Statements that reach Environment indirectly; none may leak org B's row."""
    both = Organization.id.in_([org_a.id, org_b.id])
    return {
        "count": select(func.count()).select_from(Environment),
        "join": select(Organization.id)
        .join(Environment, Environment.organization_id == Organization.id)
        .where(both),
        "exists": select(Organization.id).where(
            both, exists().where(Environment.organization_id == Organization.id)
        ),
        "in": select(Organization.id).where(
            both, Organization.id.in_(select(Environment.organization_id))
        ),
        "scalar_subquery": select(Organization.id).where(
            both,
            select(func.count())
            .select_from(Environment)
            .where(Environment.organization_id == Organization.id)
            .scalar_subquery()
            > 0,
        ),
    }


async def test_query_without_context_raises(db_session: AsyncSession) -> None:
    with pytest.raises(TenantContextError):
        await db_session.execute(select(Environment))


async def test_indirect_shapes_without_context_raise(db_session: AsyncSession) -> None:
    org_a, org_b, _, _ = await seed(db_session)
    for name, stmt in shapes(org_a, org_b).items():
        with pytest.raises(TenantContextError):
            await db_session.execute(stmt)
        assert name


async def test_system_scope_is_the_escape(db_session: AsyncSession) -> None:
    _, _, env_a, env_b = await seed(db_session)
    with system_scope():
        ids = (await db_session.scalars(select(Environment.id))).all()
    assert {env_a.id, env_b.id} <= set(ids)


async def test_org_context_filters_rows(db_session: AsyncSession) -> None:
    org_a, _, env_a, env_b = await seed(db_session)
    with org_scope(ctx_for(org_a.id)):
        rows = (await db_session.scalars(select(Environment))).all()
        assert [e.id for e in rows] == [env_a.id]
        assert await get_in_org(db_session, Environment, env_a.id) is not None
        assert await get_in_org(db_session, Environment, env_b.id) is None


async def test_indirect_shapes_are_filtered(db_session: AsyncSession) -> None:
    org_a, org_b, _, _ = await seed(db_session)
    with org_scope(ctx_for(org_a.id)):
        assert await db_session.scalar(shapes(org_a, org_b)["count"]) == 1
        for name, stmt in shapes(org_a, org_b).items():
            if name == "count":
                continue
            assert (await db_session.scalars(stmt)).all() == [org_a.id], name


async def test_core_table_access_is_refused(db_session: AsyncSession) -> None:
    org_a, _, _, _ = await seed(db_session)
    table = Environment.__table__
    with org_scope(ctx_for(org_a.id)):
        with pytest.raises(TenantContextError):
            await db_session.execute(select(table))
        with pytest.raises(TenantContextError):
            await db_session.execute(
                select(Organization.id).where(Organization.id.in_(select(table.c.organization_id)))
            )
        with pytest.raises(TenantContextError):  # ORM select mixed with a Core FROM
            await db_session.execute(select(Organization.id).select_from(table))
        with pytest.raises(TenantContextError):  # Core column in an ORM WHERE
            await db_session.execute(select(Organization.id).where(table.c.name == "dev"))


async def test_bulk_update_and_delete_are_filtered(db_session: AsyncSession) -> None:
    org_a, org_b, _, env_b = await seed(db_session)
    principal = Principal(kind="service")
    db_session.add(principal)
    await db_session.flush()
    with system_scope():
        member = Membership(organization_id=org_b.id, principal_id=principal.id, role="viewer")
        db_session.add(member)
        await db_session.flush()
    with org_scope(ctx_for(org_a.id)):
        await db_session.execute(update(Environment).values(name="renamed"))
        await db_session.execute(delete(Membership))  # aegis_api may only hard-delete memberships
    with system_scope():
        assert (await db_session.scalars(select(Membership.id))).all() == [member.id]
        assert (
            await db_session.scalar(select(Environment.name).where(Environment.id == env_b.id))
            == "dev"
        )


async def test_flush_rejects_cross_org_writes(db_session: AsyncSession) -> None:
    org_a, org_b, _, env_b = await seed(db_session)
    with org_scope(ctx_for(org_a.id)):
        # insert into another org
        db_session.add(Environment(organization_id=org_b.id, name="x", kind="staging"))
        with pytest.raises(TenantContextError):
            await db_session.flush()
        db_session.expunge_all()

    with system_scope():
        loaded = await db_session.get(Environment, env_b.id)
    assert loaded is not None
    with org_scope(ctx_for(org_a.id)):
        loaded.name = "hijacked"  # update of another org's row
        with pytest.raises(TenantContextError):
            await db_session.flush()
        db_session.expire_all()
    with system_scope():
        loaded = await db_session.get(Environment, env_b.id)
        assert loaded is not None
    with org_scope(ctx_for(org_a.id)):
        await db_session.delete(loaded)  # delete of another org's row
        with pytest.raises(TenantContextError):
            await db_session.flush()


async def test_flush_without_context_raises(db_session: AsyncSession) -> None:
    org_a, _, _, _ = await seed(db_session)
    db_session.add(Environment(organization_id=org_a.id, name="x", kind="staging"))
    with pytest.raises(TenantContextError):
        await db_session.flush()


async def test_organization_id_cannot_change_even_in_system_scope(
    db_session: AsyncSession,
) -> None:
    org_a, org_b, env_a, _ = await seed(db_session)
    with system_scope():
        env_a.organization_id = org_b.id
        with pytest.raises(TenantContextError):
            await db_session.flush()
    assert org_a.id != org_b.id


async def test_get_identity_map_hit_is_verified(db_session: AsyncSession) -> None:
    org_a, org_b, env_a, env_b = await seed(db_session)  # both now in the identity map
    with pytest.raises(TenantContextError):
        await db_session.get(Environment, env_a.id)  # no context
    with org_scope(ctx_for(org_a.id)):
        assert await db_session.get(Environment, env_a.id) is env_a
        with pytest.raises(TenantContextError):
            await db_session.get(Environment, env_b.id)  # identity-map hit, other org
        with pytest.raises(TenantContextError):
            await db_session.merge(env_b)
    with system_scope():
        assert await db_session.get(Environment, env_b.id) is env_b
    assert org_b.id != org_a.id


async def test_set_config_applied_when_transaction_begins(db_session: AsyncSession) -> None:
    org_id = new_id()
    await db_session.rollback()  # the next statement starts a fresh session transaction
    with org_scope(ctx_for(org_id)):
        got = await db_session.scalar(text("SELECT current_setting('app.org_id', true)"))
    assert got == str(org_id)


async def test_bind_org_applies_to_open_transaction_and_resets(db_session: AsyncSession) -> None:
    await db_session.execute(text("SELECT 1"))  # transaction already open, no org yet
    org_id = new_id()
    async with bind_org(db_session, ctx_for(org_id)):
        got = await db_session.scalar(text("SELECT current_setting('app.org_id', true)"))
        assert got == str(org_id)
    with pytest.raises(TenantContextError):
        await db_session.execute(select(Environment))


@pytest.fixture
async def bind_client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = FastAPI()

    org_id = new_id()

    async def org_dep() -> AsyncIterator[OrgContext]:
        async with bind_org(db_session, ctx_for(org_id)) as ctx:
            yield ctx

    org_dependency = Depends(org_dep)

    @app.get("/bound")
    async def bound(_: Annotated[OrgContext, org_dependency]) -> dict[str, int]:
        rows = await db_session.scalars(select(Environment))
        return {"n": len(rows.all())}

    @app.get("/unbound")
    async def unbound() -> dict[str, int]:
        rows = await db_session.scalars(select(Environment))
        return {"n": len(rows.all())}

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=True), base_url="http://t"
    ) as c:
        yield c


async def test_bound_org_does_not_leak_into_next_request(bind_client: AsyncClient) -> None:
    assert (await bind_client.get("/bound")).status_code == 200
    with pytest.raises(TenantContextError):
        await bind_client.get("/unbound")
