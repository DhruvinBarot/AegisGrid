from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.ids import decode_id
from app.core.tenancy import OrgContext
from app.modules.identity import sa_service as service
from app.modules.identity.sa_schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    ServiceAccountCreate,
    ServiceAccountOut,
)
from app.modules.organizations.deps import audit_meta, require

router = APIRouter(prefix="/v1/orgs/{org}/service-accounts")

Session = Annotated[AsyncSession, Depends(get_session)]
Writer = Annotated[OrgContext, Depends(require("apikeys:write"))]
Reader = Annotated[OrgContext, Depends(require("members:read"))]


@router.post("", status_code=201)
async def create_service_account(
    body: ServiceAccountCreate, request: Request, ctx: Writer, session: Session
) -> ServiceAccountOut:
    sa, role = await service.create_service_account(
        session, ctx, body.name, body.role, audit_meta(request)
    )
    out = ServiceAccountOut.of(sa, role)
    await session.commit()
    return out


@router.get("")
async def list_service_accounts(_: Reader, session: Session) -> list[ServiceAccountOut]:
    return [ServiceAccountOut.of(sa, r) for sa, r in await service.list_service_accounts(session)]


@router.delete("/{sa}", status_code=204)
async def delete_service_account(
    sa: str, request: Request, ctx: Writer, session: Session
) -> Response:
    await service.delete_service_account(session, ctx, decode_id("sa", sa), audit_meta(request))
    await session.commit()
    return Response(status_code=204)


@router.post("/{sa}/keys", status_code=201)
async def create_key(
    sa: str, body: ApiKeyCreate, request: Request, ctx: Writer, session: Session
) -> ApiKeyCreated:
    env_id = decode_id("env", body.environment_id) if body.environment_id else None
    key, full = await service.create_key(
        session, ctx, decode_id("sa", sa), body, env_id, audit_meta(request)
    )
    out = ApiKeyCreated(**ApiKeyOut.of(key).model_dump(), key=full)
    await session.commit()
    return out


@router.get("/{sa}/keys")
async def list_keys(sa: str, _: Reader, session: Session) -> list[ApiKeyOut]:
    return [ApiKeyOut.of(k) for k in await service.list_keys(session, decode_id("sa", sa))]


@router.delete("/{sa}/keys/{key}", status_code=204)
async def revoke_key(
    sa: str, key: str, request: Request, ctx: Writer, session: Session
) -> Response:
    await service.revoke_key(
        session, ctx, decode_id("sa", sa), decode_id("key", key), audit_meta(request)
    )
    await session.commit()
    return Response(status_code=204)
