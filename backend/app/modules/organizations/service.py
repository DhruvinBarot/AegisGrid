import re
import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.core.tenancy import OrgContext, system_scope
from app.modules.audit import service as audit
from app.modules.environments.models import Environment
from app.modules.organizations.deps import AuditMeta
from app.modules.organizations.models import Membership, Organization
from app.modules.organizations.schemas import OrgCreate, OrgUpdate

# (name and kind, protected)
_SEED_ENVIRONMENTS = (("development", False), ("staging", False), ("production", True))


def _default_slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30].strip("-") or "org"
    return f"{base}-{secrets.token_hex(3)}"


async def create_org(
    session: AsyncSession, principal: Principal, body: OrgCreate, meta: AuditMeta
) -> Organization:
    if principal.kind != "user" or principal.api_key is not None:
        raise ForbiddenError("Only users can create organizations.")
    org = Organization(slug=body.slug or _default_slug(body.name), name=body.name)
    try:
        async with session.begin_nested():
            session.add(org)
            await session.flush()
    except IntegrityError:
        raise ConflictError("Slug already in use.", code="slug_taken") from None
    # An org's first rows are written before any org context can exist.
    with system_scope():
        session.add(
            Membership(organization_id=org.id, principal_id=principal.principal_id, role="owner")
        )
        session.add_all(
            Environment(organization_id=org.id, name=name, kind=name, protected=protected)
            for name, protected in _SEED_ENVIRONMENTS
        )
        await session.flush()
    await session.refresh(org)
    await audit.record(
        session,
        organization_id=org.id,
        actor_type="user",
        actor_id=principal.principal_id,
        action="org.create",
        target_type="organization",
        target_id=org.id,
        metadata={"slug": org.slug},
        **meta,
    )
    return org


async def get_org(session: AsyncSession, org_id: uuid.UUID) -> Organization:
    org = await session.get(Organization, org_id)
    if org is None or org.deleted_at is not None:
        raise NotFoundError()
    return org


async def update_org(
    session: AsyncSession, ctx: OrgContext, body: OrgUpdate, meta: AuditMeta
) -> Organization:
    org = await get_org(session, ctx.org_id)
    if body.name is not None:
        org.name = body.name
    if body.settings is not None:
        org.settings = body.settings
    await session.flush()
    await audit.record(
        session,
        organization_id=org.id,
        actor_type=ctx.actor_kind,
        actor_id=ctx.principal_id,
        action="org.update",
        target_type="organization",
        target_id=org.id,
        metadata={"fields": sorted(body.model_fields_set & {"name", "settings"})},
        **meta,
    )
    return org


async def delete_org(session: AsyncSession, ctx: OrgContext, meta: AuditMeta) -> None:
    org = await get_org(session, ctx.org_id)
    org.deleted_at = datetime.now(UTC)
    await session.flush()
    await audit.record(
        session,
        organization_id=org.id,
        actor_type=ctx.actor_kind,
        actor_id=ctx.principal_id,
        action="org.delete",
        target_type="organization",
        target_id=org.id,
        **meta,
    )


async def my_organizations(
    session: AsyncSession, principal: Principal
) -> list[tuple[Organization, str]]:
    stmt = (
        select(Organization, Membership.role)
        .join(Membership, Membership.organization_id == Organization.id)
        .where(Membership.principal_id == principal.principal_id, Organization.deleted_at.is_(None))
        .order_by(Organization.id)
    )
    if principal.api_key is not None:
        stmt = stmt.where(Organization.id == principal.api_key.organization_id)
    with system_scope():  # memberships are tenant rows and the org is not known yet
        return [(o, role) for o, role in (await session.execute(stmt)).all()]
