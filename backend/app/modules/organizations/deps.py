from collections.abc import AsyncIterator, Callable
from typing import Annotated, TypedDict

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, get_current_principal
from app.core.db import get_session
from app.core.errors import ForbiddenError, NotFoundError
from app.core.ids import decode_id
from app.core.tenancy import OrgContext, TenantMixin, bind_org, system_scope
from app.modules.organizations.models import Membership, Organization
from app.modules.organizations.rbac import permissions_for


class AuditMeta(TypedDict):
    ip: str | None
    user_agent: str | None
    request_id: str | None


def audit_meta(request: Request) -> AuditMeta:
    return {
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        "request_id": request.headers.get("x-request-id"),
    }


async def reload(session: AsyncSession, obj: TenantMixin) -> None:
    """Re-read server-side defaults after a flush (`Session.refresh` is blocked by the guard)."""
    model = type(obj)
    stmt = select(model).where(model.id == obj.id).execution_options(populate_existing=True)
    await session.execute(stmt)


def require(permission: str) -> Callable[..., AsyncIterator[OrgContext]]:
    """The only way an org route obtains an OrgContext. Use as `Depends(require("members:read"))`.

    Rules, in order:
    1. The caller comes from `get_current_principal` (401 if unauthenticated).
    2. The `{org}` path id must decode, and the org must exist and not be soft-deleted, else 404.
    3. An API-key caller may only address the key's own org; any other org is 404.
    4. The caller must hold a membership in the org (looked up under `system_scope`, because no
       org context exists yet), else 404. Never 403 across orgs: an org's existence is not leaked.
    5. Permissions are the member's role permissions, intersected with the key's scopes for
       API-key callers. `environment_id` comes from the key; `actor_kind` is "api_key" or "user".
    6. The context is bound for the rest of the request, so later queries are org-filtered; if
       `permission` is not held the request is refused with 403.
    """

    async def dependency(
        org: str,
        principal: Annotated[Principal, Depends(get_current_principal)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> AsyncIterator[OrgContext]:
        org_id = decode_id("org", org)
        grant = principal.api_key
        if grant is not None and grant.organization_id != org_id:
            raise NotFoundError()
        with system_scope():
            organization = await session.get(Organization, org_id)
            if organization is None or organization.deleted_at is not None:
                raise NotFoundError()
            membership = await session.scalar(
                select(Membership).where(
                    Membership.organization_id == org_id,
                    Membership.principal_id == principal.principal_id,
                )
            )
        if membership is None:
            raise NotFoundError()
        permissions = permissions_for(membership.role)
        if grant is not None:
            permissions &= grant.scopes
        ctx = OrgContext(
            org_id=org_id,
            principal_id=principal.principal_id,
            role=membership.role,
            permissions=permissions,
            environment_id=grant.environment_id if grant else None,
            actor_kind="api_key" if grant else "user",
        )
        async with bind_org(session, ctx):
            if permission not in permissions:
                raise ForbiddenError(f"Missing permission: {permission}.")
            yield ctx

    return dependency
