import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.ids import decode_id, encode_id
from app.core.tenancy import OrgContext
from app.modules.agents import deployments, service
from app.modules.agents.schemas import (
    AgentCreate,
    AgentOut,
    AgentUpdate,
    DeployIn,
    DeploymentOut,
    DraftOut,
    DraftUpdate,
    Page,
    PublishIn,
    RollbackIn,
    ValidationOut,
    VersionDetail,
    VersionOut,
)
from app.modules.organizations.deps import audit_meta, require

router = APIRouter(prefix="/v1/orgs/{org}")

Session = Annotated[AsyncSession, Depends(get_session)]
Read = Annotated[OrgContext, Depends(require("agents:read"))]
Write = Annotated[OrgContext, Depends(require("agents:write"))]
Publish = Annotated[OrgContext, Depends(require("agents:publish"))]
Deploy = Annotated[OrgContext, Depends(require("agents:deploy"))]
Limit = Annotated[int, Query(ge=1, le=100)]


def _next[T](
    items: list[T], limit: int, prefix: str, id_of: Callable[[T], uuid.UUID]
) -> str | None:
    return encode_id(prefix, id_of(items[limit - 1])) if len(items) > limit else None


@router.post("/agents", status_code=201, response_model=AgentOut)
async def create_agent(
    body: AgentCreate, request: Request, ctx: Write, session: Session
) -> AgentOut:
    out = AgentOut.of(await service.create_agent(session, ctx, body, audit_meta(request)))
    await session.commit()
    return out


@router.get("/agents", response_model=Page[AgentOut])
async def list_agents(
    _: Read, session: Session, limit: Limit = 50, cursor: str | None = None
) -> Page[AgentOut]:
    rows = await service.list_agents(session, limit, service.input_id("agt", cursor, "cursor"))
    return Page(
        items=[AgentOut.of(a) for a in rows[:limit]],
        next_cursor=_next(rows, limit, "agt", lambda a: a.id),
    )


@router.get("/agents/{agent}", response_model=AgentOut)
async def get_agent(agent: str, _: Read, session: Session) -> AgentOut:
    return AgentOut.of(await service.get_agent(session, decode_id("agt", agent)))


@router.patch("/agents/{agent}", response_model=AgentOut)
async def update_agent(
    agent: str, body: AgentUpdate, request: Request, ctx: Write, session: Session
) -> AgentOut:
    updated = await service.update_agent(
        session, ctx, decode_id("agt", agent), body, audit_meta(request)
    )
    out = AgentOut.of(updated)
    await session.commit()
    return out


@router.get("/agents/{agent}/draft", response_model=DraftOut)
async def get_draft(agent: str, _: Read, session: Session) -> DraftOut:
    return DraftOut.of(await service.get_agent(session, decode_id("agt", agent)))


@router.put("/agents/{agent}/draft", response_model=DraftOut)
async def update_draft(
    agent: str, body: DraftUpdate, request: Request, ctx: Write, session: Session
) -> DraftOut:
    updated = await service.update_draft(
        session, ctx, decode_id("agt", agent), body, audit_meta(request)
    )
    out = DraftOut.of(updated)
    await session.commit()
    return out


@router.post("/agents/{agent}/draft/validate", response_model=ValidationOut)
async def validate_draft(agent: str, _: Read, session: Session) -> ValidationOut:
    """Dry run of publish: never writes, reports every problem and the would-be content_hash."""
    found = await service.get_agent(session, decode_id("agt", agent))
    return ValidationOut.of(service.validate_draft(found))


@router.post("/agents/{agent}/versions", status_code=201, response_model=VersionOut)
async def publish(
    agent: str, body: PublishIn, request: Request, ctx: Publish, session: Session
) -> VersionOut:
    version = await service.publish(
        session, ctx, decode_id("agt", agent), body, audit_meta(request)
    )
    out = VersionOut.of(version)
    await session.commit()
    return out


@router.get("/agents/{agent}/versions", response_model=Page[VersionOut])
async def list_versions(
    agent: str, _: Read, session: Session, limit: Limit = 50, cursor: str | None = None
) -> Page[VersionOut]:
    rows = await service.list_versions(
        session, decode_id("agt", agent), limit, service.input_id("ver", cursor, "cursor")
    )
    return Page(
        items=[VersionOut.of(v) for v in rows[:limit]],
        next_cursor=_next(rows, limit, "ver", lambda v: v.id),
    )


@router.get("/agents/{agent}/versions/{version}", response_model=VersionDetail)
async def get_version(agent: str, version: str, _: Read, session: Session) -> VersionDetail:
    found = await service.get_version(session, decode_id("agt", agent), decode_id("ver", version))
    return VersionDetail.of(found)


@router.post("/environments/{env}/deployments", status_code=201, response_model=DeploymentOut)
async def deploy(
    env: str, body: DeployIn, request: Request, ctx: Deploy, session: Session
) -> DeploymentOut:
    dep, version_no = await deployments.deploy(
        session, ctx, decode_id("env", env), body, audit_meta(request)
    )
    out = DeploymentOut.of(dep, version_no, active=True)
    await session.commit()
    return out


@router.get("/agents/{agent}/deployments", response_model=Page[DeploymentOut])
async def list_deployments(
    agent: str,
    ctx: Read,
    session: Session,
    limit: Limit = 50,
    cursor: str | None = None,
    environment_id: str | None = None,
) -> Page[DeploymentOut]:
    rows, active = await deployments.list_deployments(
        session,
        ctx,
        decode_id("agt", agent),
        service.input_id("env", environment_id, "environment_id"),
        limit,
        service.input_id("dep", cursor, "cursor"),
    )
    return Page(
        items=[DeploymentOut.of(d, n, d.id in active) for d, n in rows[:limit]],
        next_cursor=_next(rows, limit, "dep", lambda row: row[0].id),
    )


@router.post("/deployments/{deployment}/rollback", status_code=201, response_model=DeploymentOut)
async def rollback(
    deployment: str, body: RollbackIn, request: Request, ctx: Deploy, session: Session
) -> DeploymentOut:
    dep, version_no = await deployments.rollback(
        session, ctx, decode_id("dep", deployment), body, audit_meta(request)
    )
    out = DeploymentOut.of(dep, version_no, active=True)
    await session.commit()
    return out
