"""Service accounts and API keys. Never commits (routers do).

A service account is a principal(kind=service) + a service_accounts row + a membership in the
same org, created together. It has no soft-delete column, so "deleted" means: keys revoked and
membership removed (it then resolves to no org context and is hidden from the list).
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ProblemError
from app.core.tenancy import OrgContext, get_in_org
from app.modules.audit.service import record
from app.modules.environments.models import Environment
from app.modules.identity import api_keys
from app.modules.identity.models import Principal
from app.modules.identity.sa_schemas import ApiKeyCreate
from app.modules.organizations.deps import AuditMeta, reload
from app.modules.organizations.models import ApiKey, Membership, ServiceAccount
from app.modules.organizations.rbac import can_grant, permissions_for

MAX_KEY_TTL = timedelta(days=365)
_FORBIDDEN_SA_ROLES = {"owner", "admin"}


def require_human(ctx: OrgContext) -> None:
    if ctx.actor_kind != "user":
        raise ForbiddenError("API keys cannot manage service accounts or keys.")


def _own_only(ctx: OrgContext, sa: ServiceAccount) -> None:
    """Developers manage only the service accounts they created."""
    if ctx.role == "developer" and sa.created_by != ctx.principal_id:
        raise ForbiddenError("Developers can only manage service accounts they created.")


async def _audit(
    session: AsyncSession,
    ctx: OrgContext,
    meta: AuditMeta,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    metadata: dict[str, object] | None = None,
) -> None:
    await record(
        session,
        organization_id=ctx.org_id,
        actor_type="user",
        actor_id=ctx.principal_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        metadata=metadata,
        **meta,
    )


async def create_service_account(
    session: AsyncSession, ctx: OrgContext, name: str, role: str, meta: AuditMeta
) -> tuple[ServiceAccount, str]:
    require_human(ctx)
    if role in _FORBIDDEN_SA_ROLES or not can_grant(ctx.role, role):
        raise ForbiddenError("That role cannot be given to a service account.")
    principal = Principal(kind="service")
    session.add(principal)
    await session.flush()
    sa = ServiceAccount(
        organization_id=ctx.org_id,
        principal_id=principal.id,
        name=name,
        created_by=ctx.principal_id,
    )
    try:
        async with session.begin_nested():
            session.add(sa)
            await session.flush()
            session.add(
                Membership(organization_id=ctx.org_id, principal_id=principal.id, role=role)
            )
            await session.flush()
    except IntegrityError:
        raise ConflictError("A service account with this name already exists.") from None
    await reload(session, sa)
    await _audit(
        session, ctx, meta, "service_account.create", "service_account", sa.id, {"role": role}
    )
    return sa, role


async def get_service_account(
    session: AsyncSession, sa_id: uuid.UUID
) -> tuple[ServiceAccount, str]:
    """Live (membership still present) service account of the current org, with its role."""
    sa = await get_in_org(session, ServiceAccount, sa_id)
    role = (
        await session.scalar(
            select(Membership.role).where(Membership.principal_id == sa.principal_id)
        )
        if sa
        else None
    )
    if sa is None or role is None:
        raise NotFoundError()
    return sa, role


async def list_service_accounts(session: AsyncSession) -> list[tuple[ServiceAccount, str]]:
    rows = await session.execute(
        select(ServiceAccount, Membership.role)
        .join(Membership, Membership.principal_id == ServiceAccount.principal_id)
        .order_by(ServiceAccount.created_at, ServiceAccount.id)
    )
    return [(sa, role) for sa, role in rows.all()]


async def _revoke_keys(session: AsyncSession, sa_id: uuid.UUID) -> None:
    await session.execute(
        update(ApiKey)
        .where(ApiKey.service_account_id == sa_id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )


async def delete_service_account(
    session: AsyncSession, ctx: OrgContext, sa_id: uuid.UUID, meta: AuditMeta
) -> None:
    require_human(ctx)
    sa, _ = await get_service_account(session, sa_id)
    _own_only(ctx, sa)
    await _revoke_keys(session, sa.id)
    await session.execute(
        delete(Membership)
        .where(Membership.principal_id == sa.principal_id)
        .execution_options(synchronize_session=False)
    )
    await _audit(session, ctx, meta, "service_account.delete", "service_account", sa.id)


async def create_key(
    session: AsyncSession,
    ctx: OrgContext,
    sa_id: uuid.UUID,
    body: ApiKeyCreate,
    environment_id: uuid.UUID | None,
    meta: AuditMeta,
) -> tuple[ApiKey, str]:
    require_human(ctx)
    sa, role = await get_service_account(session, sa_id)
    _own_only(ctx, sa)
    now = datetime.now(UTC)
    if body.expires_at.tzinfo is None:
        raise ProblemError("expires_at must include a timezone.", status=422, title="Invalid")
    if not now < body.expires_at <= now + MAX_KEY_TTL:
        raise ProblemError(
            "expires_at must be in the future and at most 1 year ahead.",
            status=422,
            title="Unprocessable Entity",
        )
    scopes = set(body.scopes)
    if not scopes <= permissions_for(role) & ctx.permissions:
        raise ForbiddenError("Scopes exceed the service account role or your own permissions.")
    env: Environment | None = None
    if environment_id is not None:
        env = await get_in_org(session, Environment, environment_id)
        if env is None or env.deleted_at is not None:
            raise NotFoundError()
    if ctx.role == "developer":
        if env is None or env.protected:
            raise ForbiddenError("Developers can only issue keys for a non-protected environment.")
    full, key_id, digest = api_keys.generate("live")
    key = ApiKey(
        organization_id=ctx.org_id,
        service_account_id=sa.id,
        environment_id=environment_id,
        key_id=key_id,
        secret_hmac=digest,
        scopes=sorted(scopes),
        expires_at=body.expires_at,
        created_by=ctx.principal_id,
    )
    session.add(key)
    await session.flush()
    await reload(session, key)
    await _audit(
        session,
        ctx,
        meta,
        "api_key.create",
        "api_key",
        key.id,
        {"service_account_id": str(sa.id), "scopes": sorted(scopes), "key_id": key_id},
    )
    return key, full


async def list_keys(session: AsyncSession, sa_id: uuid.UUID) -> list[ApiKey]:
    await get_service_account(session, sa_id)
    rows = await session.scalars(
        select(ApiKey).where(ApiKey.service_account_id == sa_id).order_by(ApiKey.created_at)
    )
    return list(rows)


async def revoke_key(
    session: AsyncSession, ctx: OrgContext, sa_id: uuid.UUID, key_pk: uuid.UUID, meta: AuditMeta
) -> None:
    require_human(ctx)
    sa, _ = await get_service_account(session, sa_id)
    _own_only(ctx, sa)
    key = await get_in_org(session, ApiKey, key_pk)
    if key is None or key.service_account_id != sa.id:
        raise NotFoundError()
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        await session.flush()
    await _audit(session, ctx, meta, "api_key.revoke", "api_key", key.id)
