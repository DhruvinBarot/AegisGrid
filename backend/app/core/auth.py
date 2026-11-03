"""Authentication contract. Workstream B (identity) implements `get_current_principal`.

Everything else codes against the dataclasses and the dependency signature below; keep both stable.
"""

import uuid
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.errors import UnauthorizedError


@dataclass(frozen=True)
class ApiKeyGrant:
    api_key_id: uuid.UUID
    organization_id: uuid.UUID
    environment_id: uuid.UUID | None
    scopes: frozenset[str]


@dataclass(frozen=True)
class Principal:
    """Who is calling. Org and role are NOT here: they come from the URL org plus a membership
    lookup (see OrgContext). `api_key` is set only for service callers authenticated by key; then
    `user_id` and `session_id` are None."""

    principal_id: uuid.UUID
    kind: Literal["user", "service"]
    user_id: uuid.UUID | None
    session_id: uuid.UUID | None
    api_key: ApiKeyGrant | None


async def get_current_principal(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> Principal:
    """FastAPI dependency: resolve the caller from a Bearer access token or an `agk_` API key.

    Raises UnauthorizedError (401 problem+json) when credentials are missing, invalid, expired or
    the session is revoked. Every failure looks the same to the caller.
    """
    # Imported here: identity.authn needs the dataclasses above.
    from app.modules.identity.authn import authenticate_bearer

    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    principal = (
        await authenticate_bearer(session, token.strip()) if scheme.lower() == "bearer" else None
    )
    if principal is None:
        raise UnauthorizedError(
            "Invalid or missing credentials.", headers={"WWW-Authenticate": "Bearer"}
        )
    return principal
