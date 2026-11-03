import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.core.tenancy import OrgContext, get_in_org
from app.modules.audit import service as audit
from app.modules.environments.models import Environment
from app.modules.environments.schemas import EnvironmentCreate, EnvironmentUpdate
from app.modules.organizations.deps import AuditMeta, reload

_PROTECTED = "environments:protected"


def _need_protected(ctx: OrgContext) -> None:
    if _PROTECTED not in ctx.permissions:
        raise ForbiddenError(f"Missing permission: {_PROTECTED}.")


def _name_taken() -> ConflictError:
    return ConflictError("Environment name already in use.", code="environment_name_taken")


async def list_environments(session: AsyncSession, ctx: OrgContext) -> list[Environment]:
    stmt = select(Environment).where(Environment.deleted_at.is_(None)).order_by(Environment.id)
    if ctx.environment_id is not None:  # an environment-restricted API key
        stmt = stmt.where(Environment.id == ctx.environment_id)
    return list((await session.scalars(stmt)).all())


async def create_environment(
    session: AsyncSession, ctx: OrgContext, body: EnvironmentCreate, meta: AuditMeta
) -> Environment:
    if body.protected:
        _need_protected(ctx)
    env = Environment(
        organization_id=ctx.org_id,
        name=body.name,
        kind=body.kind,
        protected=body.protected,
        settings=body.settings,
    )
    try:
        async with session.begin_nested():
            session.add(env)
            await session.flush()
    except IntegrityError:
        raise _name_taken() from None
    await reload(session, env)
    await audit.record(
        session,
        organization_id=ctx.org_id,
        actor_type=ctx.actor_kind,
        actor_id=ctx.principal_id,
        action="environment.create",
        target_type="environment",
        target_id=env.id,
        metadata={"name": env.name, "kind": env.kind, "protected": env.protected},
        **meta,
    )
    return env


async def update_environment(
    session: AsyncSession,
    ctx: OrgContext,
    env_id: uuid.UUID,
    body: EnvironmentUpdate,
    meta: AuditMeta,
) -> Environment:
    env = await get_in_org(session, Environment, env_id)
    if env is None or env.deleted_at is not None:
        raise NotFoundError()
    if env.protected or (body.protected is not None and body.protected != env.protected):
        _need_protected(ctx)
    try:
        async with session.begin_nested():
            if body.name is not None:
                env.name = body.name
            if body.protected is not None:
                env.protected = body.protected
            if body.settings is not None:
                env.settings = body.settings
            await session.flush()
    except IntegrityError:
        raise _name_taken() from None
    await audit.record(
        session,
        organization_id=ctx.org_id,
        actor_type=ctx.actor_kind,
        actor_id=ctx.principal_id,
        action="environment.update",
        target_type="environment",
        target_id=env.id,
        metadata={"fields": sorted(body.model_fields_set & {"name", "protected", "settings"})},
        **meta,
    )
    return env
