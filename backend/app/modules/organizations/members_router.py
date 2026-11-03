from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, get_current_principal
from app.core.config import settings
from app.core.db import get_session
from app.core.ids import decode_id, encode_id
from app.core.tenancy import OrgContext
from app.modules.organizations import invitations, members
from app.modules.organizations.deps import audit_meta, require
from app.modules.organizations.schemas import (
    InvitationAccept,
    InvitationAccepted,
    InvitationCreate,
    InvitationOut,
    MemberOut,
    MemberUpdate,
)

router = APIRouter()

Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/orgs/{org}/members", response_model=list[MemberOut])
async def list_members(
    _: Annotated[OrgContext, Depends(require("members:read"))], session: Session
) -> list[MemberOut]:
    return [MemberOut.of(m, u) for m, u in await members.list_members(session)]


@router.patch("/orgs/{org}/members/{member}", response_model=MemberOut)
async def change_role(
    member: str,
    body: MemberUpdate,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require("members:read"))],
    session: Session,
) -> MemberOut:
    m, u = await members.change_role(
        session, ctx, decode_id("mem", member), body.role, audit_meta(request)
    )
    out = MemberOut.of(m, u)
    await session.commit()
    return out


@router.delete("/orgs/{org}/members/{member}", status_code=204)
async def remove_member(
    member: str,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require("members:read"))],
    session: Session,
) -> Response:
    # members:read is enough to reach the handler because leaving is allowed; removing someone
    # else is checked against members:write inside the service.
    await members.remove_member(session, ctx, decode_id("mem", member), audit_meta(request))
    await session.commit()
    return Response(status_code=204)


@router.post(
    "/orgs/{org}/invitations",
    status_code=201,
    response_model=InvitationOut,
    response_model_exclude_unset=True,
)
async def create_invitation(
    body: InvitationCreate,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require("members:write"))],
    session: Session,
) -> InvitationOut:
    inv, token = await invitations.create_invitation(
        session, ctx, body.email, body.role, audit_meta(request)
    )
    out = InvitationOut.of(inv, datetime.now(UTC))
    if settings.expose_invite_tokens:
        out.token = token
    await session.commit()
    return out


@router.get(
    "/orgs/{org}/invitations", response_model=list[InvitationOut], response_model_exclude_unset=True
)
async def list_invitations(
    _: Annotated[OrgContext, Depends(require("members:write"))], session: Session
) -> list[InvitationOut]:
    now = datetime.now(UTC)
    return [InvitationOut.of(i, now) for i in await invitations.list_invitations(session)]


@router.delete("/orgs/{org}/invitations/{invitation}", status_code=204)
async def revoke_invitation(
    invitation: str,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require("members:write"))],
    session: Session,
) -> Response:
    await invitations.revoke_invitation(
        session, ctx, decode_id("inv", invitation), audit_meta(request)
    )
    await session.commit()
    return Response(status_code=204)


@router.post("/invitations/accept", response_model=InvitationAccepted)
async def accept_invitation(
    body: InvitationAccept,
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Session,
) -> InvitationAccepted:
    inv = await invitations.accept_invitation(session, principal, body.token, audit_meta(request))
    out = InvitationAccepted(
        organization_id=encode_id("org", inv.organization_id),
        role=inv.role,  # pyright: ignore[reportArgumentType]
    )
    await session.commit()
    return out
