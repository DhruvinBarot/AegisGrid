from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.ids import decode_id
from app.core.tenancy import OrgContext
from app.modules.environments import service
from app.modules.environments.schemas import EnvironmentCreate, EnvironmentOut, EnvironmentUpdate
from app.modules.organizations.deps import audit_meta, require

router = APIRouter(prefix="/v1/orgs/{org}/environments")

Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("", response_model=list[EnvironmentOut])
async def list_environments(
    ctx: Annotated[OrgContext, Depends(require("environments:read"))], session: Session
) -> list[EnvironmentOut]:
    return [EnvironmentOut.of(e) for e in await service.list_environments(session, ctx)]


@router.post("", status_code=201, response_model=EnvironmentOut)
async def create_environment(
    body: EnvironmentCreate,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require("environments:write"))],
    session: Session,
) -> EnvironmentOut:
    env = await service.create_environment(session, ctx, body, audit_meta(request))
    out = EnvironmentOut.of(env)
    await session.commit()
    return out


@router.patch("/{env}", response_model=EnvironmentOut)
async def update_environment(
    env: str,
    body: EnvironmentUpdate,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require("environments:write"))],
    session: Session,
) -> EnvironmentOut:
    updated = await service.update_environment(
        session, ctx, decode_id("env", env), body, audit_meta(request)
    )
    out = EnvironmentOut.of(updated)
    await session.commit()
    return out
