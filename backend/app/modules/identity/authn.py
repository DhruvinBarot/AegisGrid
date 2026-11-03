"""Bearer credential resolution behind `core.auth.get_current_principal`.

Every failure returns None; the dependency turns it into one generic 401.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import ApiKeyGrant, Principal
from app.core.tenancy import system_scope
from app.modules.identity import api_keys, tokens
from app.modules.identity.models import AuthSession
from app.modules.identity.models import Principal as PrincipalRow
from app.modules.organizations.models import ApiKey, ServiceAccount

# Compared against when the key id is unknown, so lookup misses cost the same as wrong secrets.
_MISS = b"\0" * 32


async def authenticate_bearer(session: AsyncSession, token: str) -> Principal | None:
    if token.startswith(api_keys.PREFIX + "_"):
        return await _from_api_key(session, token)
    return await _from_jwt(session, token)


async def _from_jwt(session: AsyncSession, token: str) -> Principal | None:
    claims = await tokens.verify_access_token(session, token)
    if claims is None:
        return None
    try:
        sub, sid = uuid.UUID(claims["sub"]), uuid.UUID(claims["sid"])
    except (ValueError, TypeError):
        return None
    row = (
        await session.execute(
            select(AuthSession, PrincipalRow)
            .join(PrincipalRow, PrincipalRow.id == AuthSession.principal_id)
            .where(AuthSession.id == sid)
        )
    ).one_or_none()
    if row is None:
        return None
    auth_session, principal = row
    if (
        principal.id != sub
        or principal.kind != "user"
        or principal.user_id is None
        or auth_session.revoked_at is not None
        or auth_session.absolute_expires_at <= datetime.now(UTC)
    ):
        return None
    # last_seen_at is not touched: a write per request isn't worth it yet.
    return Principal(
        principal_id=principal.id,
        kind="user",
        user_id=principal.user_id,
        session_id=auth_session.id,
        api_key=None,
    )


async def _from_api_key(session: AsyncSession, raw: str) -> Principal | None:
    parsed = api_keys.parse(raw)
    if parsed is None:
        return None
    key_id, secret = parsed
    with system_scope():  # no org context exists before the key identifies its org
        row = (
            await session.execute(
                select(ApiKey, ServiceAccount.principal_id)
                .join(
                    ServiceAccount,
                    (ServiceAccount.organization_id == ApiKey.organization_id)
                    & (ServiceAccount.id == ApiKey.service_account_id),
                )
                .where(ApiKey.key_id == key_id)
            )
        ).one_or_none()
    key, principal_id = row if row else (None, None)
    ok = api_keys.secret_matches(secret, key.secret_hmac if key else _MISS)
    if key is None or principal_id is None or not ok:
        return None
    if key.revoked_at is not None or key.expires_at <= datetime.now(UTC):
        return None
    return Principal(
        principal_id=principal_id,
        kind="service",
        user_id=None,
        session_id=None,
        api_key=ApiKeyGrant(
            api_key_id=key.id,
            organization_id=key.organization_id,
            environment_id=key.environment_id,
            scopes=frozenset(key.scopes),
        ),
    )
