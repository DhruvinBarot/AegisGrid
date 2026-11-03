"""Deploy and rollback (ADR-0007): append a deployment row and move the per-environment pointer
in one transaction. History rows are never updated; rollback is a new row."""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.core.tenancy import OrgContext, get_in_org
from app.modules.agents.models import AgentDeployment, AgentEnvironmentState, AgentVersion
from app.modules.agents.schemas import DeployIn, RollbackIn
from app.modules.agents.service import get_agent, input_id, page, record
from app.modules.environments.models import Environment
from app.modules.organizations.deps import AuditMeta, reload

_PROTECTED = "environments:protected"


async def _target_env(session: AsyncSession, ctx: OrgContext, env_id: uuid.UUID) -> Environment:
    env = await get_in_org(session, Environment, env_id)
    # An environment-restricted API key cannot see other environments at all.
    hidden = ctx.environment_id is not None and env_id != ctx.environment_id
    if env is None or env.deleted_at is not None or hidden:
        raise NotFoundError()
    if env.protected and _PROTECTED not in ctx.permissions:
        raise ForbiddenError(
            "Deploying to a protected environment requires an owner or admin.",
            code="protected_environment",
        )
    return env


async def deploy(
    session: AsyncSession, ctx: OrgContext, env_id: uuid.UUID, body: DeployIn, meta: AuditMeta
) -> tuple[AgentDeployment, int]:
    env = await _target_env(session, ctx, env_id)
    version_id = input_id("ver", body.version_id, "version_id")
    assert version_id is not None
    version = await get_in_org(session, AgentVersion, version_id)
    if version is None:
        raise NotFoundError()
    await get_agent(session, version.agent_id)  # a deleted agent cannot be deployed
    return await _append(session, ctx, env, version, "deploy", body.notes, meta)


async def rollback(
    session: AsyncSession,
    ctx: OrgContext,
    deployment_id: uuid.UUID,
    body: RollbackIn,
    meta: AuditMeta,
) -> tuple[AgentDeployment, int]:
    """Re-deploy the version of an earlier deployment as a new 'rollback' row."""
    target = await get_in_org(session, AgentDeployment, deployment_id)
    if target is None:
        raise NotFoundError()
    env = await _target_env(session, ctx, target.environment_id)
    await get_agent(session, target.agent_id)
    version = await get_in_org(session, AgentVersion, target.agent_version_id)
    assert version is not None  # composite FK
    return await _append(session, ctx, env, version, "rollback", body.notes, meta)


async def _append(
    session: AsyncSession,
    ctx: OrgContext,
    env: Environment,
    version: AgentVersion,
    reason: str,
    notes: str,
    meta: AuditMeta,
) -> tuple[AgentDeployment, int]:
    # Lock the pointer row: concurrent deploys to one (agent, env) run one after the other.
    state = await session.scalar(
        select(AgentEnvironmentState)
        .where(
            AgentEnvironmentState.agent_id == version.agent_id,
            AgentEnvironmentState.environment_id == env.id,
        )
        .with_for_update()
    )
    current_id = state.active_deployment_id if state else None
    if current_id is not None:
        current = await get_in_org(session, AgentDeployment, current_id)
        if current is not None and current.agent_version_id == version.id:
            raise ConflictError(
                f"Version {version.version_no} is already active in this environment.",
                code="already_active",
            )
    deployment = AgentDeployment(
        organization_id=ctx.org_id,
        agent_id=version.agent_id,
        environment_id=env.id,
        agent_version_id=version.id,
        reason=reason,
        supersedes_id=current_id,
        notes=notes,
        deployed_by=ctx.principal_id,
    )
    try:
        async with session.begin_nested():
            session.add(deployment)
            await session.flush()
            if state is None:  # first deploy of this agent here; a racing first deploy hits the
                session.add(  # UNIQUE(agent, env) and gets the 409 below
                    AgentEnvironmentState(
                        organization_id=ctx.org_id,
                        agent_id=version.agent_id,
                        environment_id=env.id,
                        active_deployment_id=deployment.id,
                    )
                )
            else:
                state.active_deployment_id = deployment.id
            await session.flush()
    except IntegrityError:
        raise ConflictError(
            "Another deployment to this environment happened at the same time; retry.",
            code="deployment_conflict",
        ) from None
    await reload(session, deployment)
    await record(
        session,
        ctx,
        meta,
        f"agent.{reason}",
        "agent_deployment",
        deployment.id,
        agent_id=str(version.agent_id),
        environment_id=str(env.id),
        version_no=version.version_no,
        supersedes_id=str(current_id) if current_id else None,
    )
    return deployment, version.version_no


async def list_deployments(
    session: AsyncSession,
    ctx: OrgContext,
    agent_id: uuid.UUID,
    env_id: uuid.UUID | None,
    limit: int,
    before: uuid.UUID | None,
) -> tuple[list[tuple[AgentDeployment, int]], set[uuid.UUID]]:
    """A page of history (newest first) with each row's version_no, plus the ids that are live."""
    await get_agent(session, agent_id)
    stmt = (
        select(AgentDeployment, AgentVersion.version_no)
        .join(AgentVersion, AgentVersion.id == AgentDeployment.agent_version_id)
        .where(AgentDeployment.agent_id == agent_id)
    )
    for restrict in (env_id, ctx.environment_id):
        if restrict is not None:
            stmt = stmt.where(AgentDeployment.environment_id == restrict)
    rows = (await session.execute(page(stmt, AgentDeployment.id, before, limit))).all()
    active = await session.scalars(
        select(AgentEnvironmentState.active_deployment_id).where(
            AgentEnvironmentState.agent_id == agent_id
        )
    )
    return [(d, n) for d, n in rows], set(active.all())
