from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, get_current_principal
from app.core.db import get_session
from app.core.tenancy import OrgContext
from app.modules.organizations import service
from app.modules.organizations.deps import audit_meta, require
from app.modules.organizations.members_router import router as members_router
from app.modules.organizations.schemas import MyOrgOut, OrgCreate, OrgOut, OrgUpdate

router = APIRouter(prefix="/v1")
router.include_router(members_router)

Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/me/organizations", response_model=list[MyOrgOut])
async def my_organizations(
    principal: Annotated[Principal, Depends(get_current_principal)], session: Session
) -> list[MyOrgOut]:
    rows = await service.my_organizations(session, principal)
    return [MyOrgOut(organization=OrgOut.of(o), role=role) for o, role in rows]  # pyright: ignore[reportArgumentType]


@router.post("/orgs", status_code=201, response_model=OrgOut)
async def create_org(
    body: OrgCreate,
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Session,
) -> OrgOut:
    org = await service.create_org(session, principal, body, audit_meta(request))
    out = OrgOut.of(org)
    await session.commit()
    return out


@router.get("/orgs/{org}", response_model=OrgOut)
async def get_org(
    ctx: Annotated[OrgContext, Depends(require("org:read"))], session: Session
) -> OrgOut:
    return OrgOut.of(await service.get_org(session, ctx.org_id))


@router.patch("/orgs/{org}", response_model=OrgOut)
async def update_org(
    body: OrgUpdate,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require("org:write"))],
    session: Session,
) -> OrgOut:
    out = OrgOut.of(await service.update_org(session, ctx, body, audit_meta(request)))
    await session.commit()
    return out


@router.delete("/orgs/{org}", status_code=204)
async def delete_org(
    request: Request,
    ctx: Annotated[OrgContext, Depends(require("org:delete"))],
    session: Session,
) -> Response:
    await service.delete_org(session, ctx, audit_meta(request))
    await session.commit()
    return Response(status_code=204)
