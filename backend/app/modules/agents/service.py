"""Agents, drafts and publishing. Routers commit; everything here only flushes, so each change
and its audit row commit or roll back together."""

import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ProblemError
from app.core.ids import decode_id
from app.core.tenancy import OrgContext
from app.modules.agents.models import Agent, AgentVersion
from app.modules.agents.schemas import AgentCreate, AgentUpdate, DraftUpdate, PublishIn
from app.modules.agents.spec import MAX_SPEC_BYTES, SPEC_SCHEMA_VERSION, Issue, Result
from app.modules.agents.spec import validate_spec as _validate_spec
from app.modules.audit import service as audit
from app.modules.organizations.deps import AuditMeta, reload


async def record(
    session: AsyncSession,
    ctx: OrgContext,
    meta: AuditMeta,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    **metadata: Any,
) -> None:
    await audit.record(
        session,
        organization_id=ctx.org_id,
        actor_type=ctx.actor_kind,
        actor_id=ctx.principal_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        metadata=metadata,
        **meta,
    )


def input_id(prefix: str, value: str | None, name: str) -> uuid.UUID | None:
    """Decode an id given in a query string or body. Malformed is a 422, not a 404: it is input,
    not the resource the URL addresses."""
    if value is None:
        return None
    try:
        return decode_id(prefix, value)
    except NotFoundError:
        raise ProblemError(
            f"Invalid {name}.", status=422, title="Unprocessable Entity", code=f"invalid_{name}"
        ) from None


def page[*Ts](stmt: Select[*Ts], id_col: Any, before: uuid.UUID | None, limit: int) -> Select[*Ts]:
    """Newest first by id (UUIDv7, so creation order); one extra row tells if there is more."""
    if before is not None:
        stmt = stmt.where(id_col < before)
    return stmt.order_by(id_col.desc()).limit(limit + 1)


async def get_agent(session: AsyncSession, agent_id: uuid.UUID, *, lock: bool = False) -> Agent:
    """Org-filtered by the tenancy guard; another org's agent is simply not found."""
    stmt = select(Agent).where(Agent.id == agent_id, Agent.deleted_at.is_(None))
    if lock:
        stmt = stmt.with_for_update()
    agent = (await session.execute(stmt)).scalar_one_or_none()
    if agent is None:
        raise NotFoundError()
    return agent


async def list_agents(session: AsyncSession, limit: int, before: uuid.UUID | None) -> list[Agent]:
    stmt = page(select(Agent).where(Agent.deleted_at.is_(None)), Agent.id, before, limit)
    return list((await session.scalars(stmt)).all())


async def create_agent(
    session: AsyncSession, ctx: OrgContext, body: AgentCreate, meta: AuditMeta
) -> Agent:
    agent = Agent(
        organization_id=ctx.org_id,
        slug=body.slug,
        name=body.name,
        description=body.description,
        created_by=ctx.principal_id,
    )
    try:
        async with session.begin_nested():
            session.add(agent)
            await session.flush()
    except IntegrityError:
        raise ConflictError("Agent slug already in use.", code="agent_slug_taken") from None
    await reload(session, agent)
    await record(session, ctx, meta, "agent.create", "agent", agent.id, slug=agent.slug)
    return agent


async def update_agent(
    session: AsyncSession, ctx: OrgContext, agent_id: uuid.UUID, body: AgentUpdate, meta: AuditMeta
) -> Agent:
    agent = await get_agent(session, agent_id)
    fields = sorted(body.model_fields_set & {"name", "description"})
    if body.name is not None:
        agent.name = body.name
    if body.description is not None:
        agent.description = body.description
    await session.flush()
    await reload(session, agent)
    await record(session, ctx, meta, "agent.update", "agent", agent.id, fields=fields)
    return agent


async def update_draft(
    session: AsyncSession, ctx: OrgContext, agent_id: uuid.UUID, body: DraftUpdate, meta: AuditMeta
) -> Agent:
    """Drafts are work in progress: any JSON object is stored; validity is checked on validate
    and enforced on publish. `draft_rev` is the optimistic lock."""
    if len(json.dumps(body.spec)) > MAX_SPEC_BYTES:
        raise ProblemError(
            f"Spec exceeds {MAX_SPEC_BYTES} bytes.",
            status=422,
            title="Unprocessable Entity",
            code="spec_too_large",
        )
    agent = await get_agent(session, agent_id, lock=True)
    _check_rev(agent, body.draft_rev)
    agent.draft_spec = body.spec
    agent.draft_rev += 1
    agent.draft_updated_at = datetime.now(UTC)
    await session.flush()
    await reload(session, agent)
    await record(
        session, ctx, meta, "agent.draft_update", "agent", agent.id, draft_rev=agent.draft_rev
    )
    return agent


def validate_draft(agent: Agent) -> Result:
    if agent.draft_spec is None:
        return Result(None, None, [Issue("", "empty_draft", "The draft is empty.")])
    return _validate_spec(agent.draft_spec)


async def publish(
    session: AsyncSession, ctx: OrgContext, agent_id: uuid.UUID, body: PublishIn, meta: AuditMeta
) -> AgentVersion:
    # The agent row lock serializes publishes of one agent, so max()+1 cannot race; the
    # UNIQUE(agent_id, version_no) constraint is the backstop.
    agent = await get_agent(session, agent_id, lock=True)
    if body.draft_rev is not None:
        _check_rev(agent, body.draft_rev)
    result = validate_draft(agent)
    if result.spec is None or result.content_hash is None:
        raise ProblemError(
            "The draft is not a valid spec.",
            status=422,
            title="Unprocessable Entity",
            code="invalid_spec",
            errors=[asdict(e) for e in result.errors],
        )
    last = await session.scalar(
        select(func.max(AgentVersion.version_no)).where(AgentVersion.agent_id == agent.id)
    )
    version = AgentVersion(
        organization_id=ctx.org_id,
        agent_id=agent.id,
        version_no=(last or 0) + 1,
        spec=result.spec,
        spec_schema_version=SPEC_SCHEMA_VERSION,
        content_hash=result.content_hash,
        notes=body.notes,
        created_by=ctx.principal_id,
    )
    try:
        async with session.begin_nested():
            session.add(version)
            await session.flush()
    except IntegrityError:
        raise ConflictError("A concurrent publish won; retry.", code="publish_conflict") from None
    await reload(session, version)
    await record(
        session,
        ctx,
        meta,
        "agent.publish",
        "agent_version",
        version.id,
        agent_id=str(agent.id),
        version_no=version.version_no,
        content_hash=version.content_hash,
        draft_rev=agent.draft_rev,
    )
    return version


async def list_versions(
    session: AsyncSession, agent_id: uuid.UUID, limit: int, before: uuid.UUID | None
) -> list[AgentVersion]:
    await get_agent(session, agent_id)
    stmt = page(
        select(AgentVersion).where(AgentVersion.agent_id == agent_id),
        AgentVersion.id,
        before,
        limit,
    )
    return list((await session.scalars(stmt)).all())


async def get_version(
    session: AsyncSession, agent_id: uuid.UUID, version_id: uuid.UUID
) -> AgentVersion:
    await get_agent(session, agent_id)
    version = await session.scalar(
        select(AgentVersion).where(AgentVersion.id == version_id, AgentVersion.agent_id == agent_id)
    )
    if version is None:
        raise NotFoundError()
    return version


def _check_rev(agent: Agent, expected: int) -> None:
    if agent.draft_rev != expected:
        raise ConflictError(
            "The draft changed since you loaded it.",
            code="draft_conflict",
            draft_rev=agent.draft_rev,
        )
