"""Identity use cases. Never commits: routers own the transaction (and commit before returning a
401 so failure audit rows and reuse revocations are kept)."""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ConflictError
from app.modules.audit.service import record
from app.modules.identity import tokens
from app.modules.identity.models import AuthSession, Principal, RefreshToken, User, UserIdentity
from app.modules.identity.providers import normalize_email, password_provider


@dataclass(frozen=True)
class ClientInfo:
    ip: str | None
    user_agent: str | None


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    expires_in: int
    refresh_token: str


def _now() -> datetime:
    return datetime.now(UTC)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _email_digest(email: str) -> str:
    return hashlib.sha256(email.encode()).hexdigest()[:16]


async def register(
    session: AsyncSession, *, email: str, password: str, display_name: str, client: ClientInfo
) -> User:
    email = normalize_email(email)
    user = User(email=email, display_name=display_name)
    try:
        async with session.begin_nested():
            session.add(user)
            await session.flush()
            await password_provider.add_identity(
                session, user_id=user.id, email=email, password=password
            )
            principal = Principal(kind="user", user_id=user.id)
            session.add(principal)
            await session.flush()
    except IntegrityError:
        raise ConflictError("An account with this email already exists.") from None
    await record(
        session,
        organization_id=None,
        actor_type="user",
        actor_id=principal.id,
        action="auth.register",
        target_type="user",
        target_id=user.id,
        ip=client.ip,
        user_agent=client.user_agent,
    )
    return user


async def _start_refresh(
    session: AsyncSession, auth_session: AuthSession, family_id: uuid.UUID
) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    session.add(
        RefreshToken(
            session_id=auth_session.id,
            family_id=family_id,
            token_hash=_hash(token),
            expires_at=min(
                now + timedelta(seconds=settings.refresh_idle_ttl_seconds),
                auth_session.absolute_expires_at,
            ),
        )
    )
    await session.flush()
    return token


async def _pair(
    session: AsyncSession, auth_session: AuthSession, family_id: uuid.UUID
) -> TokenPair:
    access, ttl = await tokens.issue_access_token(
        session, principal_id=auth_session.principal_id, session_id=auth_session.id
    )
    return TokenPair(access, ttl, await _start_refresh(session, auth_session, family_id))


async def login(
    session: AsyncSession, *, email: str, password: str, client: ClientInfo
) -> TokenPair | None:
    email = normalize_email(email)
    verified = await password_provider.authenticate(session, email=email, password=password)
    principal: Principal | None = None
    if verified is not None:
        principal = (
            await session.execute(
                select(Principal)
                .join(UserIdentity, UserIdentity.user_id == Principal.user_id)
                .where(
                    UserIdentity.provider == verified.provider,
                    UserIdentity.issuer == verified.issuer,
                    UserIdentity.subject == verified.subject,
                )
            )
        ).scalar_one_or_none()
    if principal is None:
        existing = (
            await session.execute(
                select(Principal.id)
                .join(User, User.id == Principal.user_id)
                .where(User.email == email)
            )
        ).scalar_one_or_none()
        await record(
            session,
            organization_id=None,
            actor_type="user" if existing else "anonymous",
            actor_id=existing,
            action="auth.login",
            outcome="failure",
            metadata={"email_sha256": _email_digest(email)},
            ip=client.ip,
            user_agent=client.user_agent,
        )
        return None
    auth_session = AuthSession(
        principal_id=principal.id,
        created_ip=client.ip,
        user_agent=client.user_agent,
        absolute_expires_at=_now() + timedelta(seconds=settings.refresh_absolute_ttl_seconds),
    )
    session.add(auth_session)
    await session.flush()
    pair = await _pair(session, auth_session, new_family := uuid.uuid4())
    await record(
        session,
        organization_id=None,
        actor_type="user",
        actor_id=principal.id,
        action="auth.login",
        target_type="session",
        target_id=auth_session.id,
        metadata={"family_id": str(new_family)},
        ip=client.ip,
        user_agent=client.user_agent,
    )
    return pair


async def refresh(
    session: AsyncSession, *, refresh_token: str, client: ClientInfo
) -> TokenPair | None:
    """Rotate a refresh token. None on any failure; reuse also revokes the family and session."""
    row = (
        await session.execute(
            select(RefreshToken)
            .where(RefreshToken.token_hash == _hash(refresh_token))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    auth_session = await session.get(AuthSession, row.session_id)
    now = _now()
    if auth_session is None:
        return None
    if row.used_at is not None:
        await _revoke_family(session, row.family_id, auth_session.id)
        await record(
            session,
            organization_id=None,
            actor_type="user",
            actor_id=auth_session.principal_id,
            action="auth.refresh_reuse_detected",
            outcome="failure",
            target_type="session",
            target_id=auth_session.id,
            metadata={"family_id": str(row.family_id)},
            ip=client.ip,
            user_agent=client.user_agent,
        )
        return None
    if (
        row.revoked_at is not None
        or row.expires_at <= now
        or auth_session.revoked_at is not None
        or auth_session.absolute_expires_at <= now
    ):
        return None
    row.used_at = now
    return await _pair(session, auth_session, row.family_id)


async def _revoke_family(
    session: AsyncSession, family_id: uuid.UUID, session_id: uuid.UUID
) -> None:
    now = _now()
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(AuthSession)
        .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )


async def revoke_sessions(
    session: AsyncSession, principal_id: uuid.UUID, *, only: uuid.UUID | None = None
) -> None:
    """Revoke one session (`only`) or all sessions of the principal, with their refresh tokens."""
    scope = [AuthSession.principal_id == principal_id]
    if only is not None:
        scope.append(AuthSession.id == only)
    now = _now()
    await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.session_id.in_(select(AuthSession.id).where(*scope)),
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(AuthSession)
        .where(*scope, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
