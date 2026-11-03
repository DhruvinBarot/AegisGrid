import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ProblemError
from app.core.tenancy import OrgContext, get_in_org
from app.modules.audit import service as audit
from app.modules.identity.models import Principal as PrincipalRow
from app.modules.identity.models import User
from app.modules.organizations.deps import AuditMeta
from app.modules.organizations.models import Invitation, Membership
from app.modules.organizations.rbac import can_grant, rank


async def list_members(session: AsyncSession) -> list[tuple[Membership, User]]:
    # The inner joins to users leave service-account memberships out (E1-8).
    stmt = (
        select(Membership, User)
        .join(PrincipalRow, PrincipalRow.id == Membership.principal_id)
        .join(User, User.id == PrincipalRow.user_id)
        .order_by(Membership.id)
    )
    return [(m, u) for m, u in (await session.execute(stmt)).all()]


async def _user_member(session: AsyncSession, member_id: uuid.UUID) -> tuple[Membership, User]:
    m = await get_in_org(session, Membership, member_id)
    if m is None:
        raise NotFoundError()
    principal = await session.get(PrincipalRow, m.principal_id)
    if principal is None or principal.user_id is None:
        raise ProblemError(
            "Service principals are managed through service accounts.",
            status=422,
            title="Unprocessable Entity",
            code="service_principal",
        )
    user = await session.get(User, principal.user_id)
    assert user is not None
    return m, user


def require_user_writer(ctx: OrgContext) -> None:
    """Member and invitation changes are for human members; API keys never manage members."""
    if ctx.actor_kind != "user" or "members:write" not in ctx.permissions:
        raise ForbiddenError("Missing permission: members:write.")


async def _ensure_another_owner(session: AsyncSession, target: Membership) -> None:
    """Lock every owner row, then refuse if `target` is the only one left.

    The lock serialises concurrent demotions; populate_existing makes the waiter see the winner's
    committed change instead of a stale identity-map copy.
    """
    owners = (
        await session.scalars(
            select(Membership)
            .where(Membership.role == "owner")
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    if target.role == "owner" and not any(o.id != target.id for o in owners):
        raise ConflictError("An organization needs at least one owner.", code="last_owner")


async def change_role(
    session: AsyncSession, ctx: OrgContext, member_id: uuid.UUID, role: str, meta: AuditMeta
) -> tuple[Membership, User]:
    require_user_writer(ctx)
    m, user = await _user_member(session, member_id)
    if m.principal_id == ctx.principal_id:
        raise ForbiddenError("You cannot change your own role.")
    if not can_grant(ctx.role, role) or rank(m.role) > rank(ctx.role):
        raise ForbiddenError("You can only manage roles at or below your own.")
    if m.role == "owner" and role != "owner":
        await _ensure_another_owner(session, m)
    previous = m.role
    if previous != role:
        m.role = role
        await session.flush()
        await audit.record(
            session,
            organization_id=ctx.org_id,
            actor_type=ctx.actor_kind,
            actor_id=ctx.principal_id,
            action="member.role_change",
            target_type="membership",
            target_id=m.id,
            metadata={"from": previous, "to": role},
            **meta,
        )
    return m, user


async def remove_member(
    session: AsyncSession, ctx: OrgContext, member_id: uuid.UUID, meta: AuditMeta
) -> None:
    m, _ = await _user_member(session, member_id)
    leaving = m.principal_id == ctx.principal_id
    if not leaving:
        require_user_writer(ctx)
        if rank(m.role) > rank(ctx.role):
            raise ForbiddenError("You can only remove members at or below your own role.")
    if m.role == "owner":
        await _ensure_another_owner(session, m)
    pending = await session.scalars(
        select(Invitation).where(
            Invitation.invited_by == m.principal_id,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
    )
    for inv in pending:
        inv.revoked_at = datetime.now(UTC)
    await session.delete(m)
    await session.flush()
    await audit.record(
        session,
        organization_id=ctx.org_id,
        actor_type=ctx.actor_kind,
        actor_id=ctx.principal_id,
        action="member.leave" if leaving else "member.remove",
        target_type="membership",
        target_id=m.id,
        metadata={"role": m.role},
        **meta,
    )
