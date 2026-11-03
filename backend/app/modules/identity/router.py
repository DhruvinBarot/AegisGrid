"""Auth endpoints. The refresh cookie is scoped to Path=/v1/auth/refresh, so the browser sends it
nowhere else; logout and logout-all authenticate with the Bearer access token instead."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, Request, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, get_current_principal
from app.core.config import settings
from app.core.db import get_session
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.ids import encode_id
from app.modules.audit.service import record
from app.modules.identity import ratelimit, service, tokens
from app.modules.identity.models import User
from app.modules.identity.providers import normalize_email
from app.modules.identity.sa_router import router as service_accounts_router
from app.modules.identity.schemas import LoginIn, MeOut, RegisterIn, TokenOut, UserOut
from app.modules.identity.service import ClientInfo, TokenPair

router = APIRouter()
router.include_router(service_accounts_router)

COOKIE = "ag_refresh"
COOKIE_PATH = "/v1/auth/refresh"
CSRF_HEADER = "X-AegisGrid-CSRF"

Db = Annotated[AsyncSession, Depends(get_session)]
Cache = Annotated[Redis, Depends(ratelimit.get_redis)]
Current = Annotated[Principal, Depends(get_current_principal)]


def csrf_guard(request: Request) -> None:
    """Cookie endpoints need a custom header (cross-site forms can't send one) and, when the
    browser sends Origin, an allow-listed origin."""
    if request.headers.get(CSRF_HEADER) != "1":
        raise ForbiddenError("Missing CSRF header.")
    origin = request.headers.get("origin")
    if origin is not None and origin not in settings.allowed_origins:
        raise ForbiddenError("Origin not allowed.")


Csrf = Depends(csrf_guard)


def _client(request: Request) -> ClientInfo:
    # shortcut: no X-Forwarded-For handling; behind a proxy this is the proxy's address.
    return ClientInfo(
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE,
        token,
        max_age=settings.refresh_idle_ttl_seconds,
        path=COOKIE_PATH,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
    )


def _clear_cookie(response: Response) -> None:
    response.delete_cookie(
        COOKIE,
        path=COOKIE_PATH,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
    )


def _tokens_out(pair: TokenPair, response: Response) -> TokenOut:
    _set_cookie(response, pair.refresh_token)
    response.headers["Cache-Control"] = "no-store"
    return TokenOut(access_token=pair.access_token, expires_in=pair.expires_in)


@router.post("/v1/auth/register", status_code=201)
async def register(body: RegisterIn, request: Request, session: Db, cache: Cache) -> UserOut:
    client = _client(request)
    await ratelimit.enforce(cache, "register", (f"ip:{client.ip}", settings.register_ip_limit))
    user = await service.register(
        session,
        email=body.email,
        password=body.password,
        display_name=body.display_name,
        client=client,
    )
    await session.commit()
    return UserOut(id=encode_id("usr", user.id), email=user.email, display_name=user.display_name)


@router.post("/v1/auth/login", dependencies=[Csrf])
async def login(
    body: LoginIn, request: Request, response: Response, session: Db, cache: Cache
) -> TokenOut:
    client = _client(request)
    await ratelimit.enforce(
        cache,
        "login",
        (f"ip:{client.ip}", settings.login_ip_limit),
        (ratelimit.email_key(normalize_email(body.email)), settings.login_email_limit),
    )
    pair = await service.login(session, email=body.email, password=body.password, client=client)
    await session.commit()  # keeps the audit row on failure too
    if pair is None:
        raise UnauthorizedError("Invalid email or password.")
    return _tokens_out(pair, response)


@router.post("/v1/auth/refresh", dependencies=[Csrf])
async def refresh(
    request: Request,
    response: Response,
    session: Db,
    ag_refresh: Annotated[str | None, Cookie()] = None,
) -> TokenOut:
    pair = (
        await service.refresh(session, refresh_token=ag_refresh, client=_client(request))
        if ag_refresh
        else None
    )
    await session.commit()  # keeps reuse revocation + audit
    if pair is None:
        raise UnauthorizedError("Invalid refresh token.")
    return _tokens_out(pair, response)


def _session_id(principal: Principal) -> uuid.UUID:
    if principal.session_id is None:
        raise ForbiddenError("API keys have no session.")
    return principal.session_id


@router.post("/v1/auth/logout", status_code=204, dependencies=[Csrf])
async def logout(request: Request, response: Response, principal: Current, session: Db) -> None:
    session_id = _session_id(principal)
    client = _client(request)
    await service.revoke_sessions(session, principal.principal_id, only=session_id)
    await record(
        session,
        organization_id=None,
        actor_type="user",
        actor_id=principal.principal_id,
        action="auth.logout",
        target_type="session",
        target_id=session_id,
        ip=client.ip,
        user_agent=client.user_agent,
    )
    await session.commit()
    _clear_cookie(response)


@router.post("/v1/auth/logout-all", status_code=204, dependencies=[Csrf])
async def logout_all(request: Request, response: Response, principal: Current, session: Db) -> None:
    _session_id(principal)
    client = _client(request)
    await service.revoke_sessions(session, principal.principal_id)
    await record(
        session,
        organization_id=None,
        actor_type="user",
        actor_id=principal.principal_id,
        action="auth.logout_all",
        ip=client.ip,
        user_agent=client.user_agent,
    )
    await session.commit()
    _clear_cookie(response)


@router.get("/.well-known/jwks.json")
async def jwks(session: Db, response: Response) -> dict[str, Any]:
    keys = await tokens.jwks(session)
    await session.commit()  # may have created the first key
    response.headers["Cache-Control"] = "public, max-age=300"
    return keys


@router.get("/v1/me")
async def me(principal: Current, session: Db) -> MeOut:
    user = await session.get(User, principal.user_id) if principal.user_id else None
    if user is None or principal.session_id is None:
        raise ForbiddenError("Only user principals have a profile.")
    return MeOut(
        id=encode_id("usr", user.id),
        email=user.email,
        display_name=user.display_name,
        principal_id=encode_id("prn", principal.principal_id),
        session_id=encode_id("ses", principal.session_id),
    )
