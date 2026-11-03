import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ProblemError
from app.core.tenancy import OrgContext, get_in_org, system_scope
from app.modules.audit import service as audit
from app.modules.identity.models import User
from app.modules.organizations.deps import AuditMeta, reload
from app.modules.organizations.members import require_user_writer
from app.modules.organizations.models import Invitation, Membership, Organization
from app.modules.organizations.rbac import can_grant, permissions_for

INVITATION_TTL = timedelta(days=7)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _invalid() -> ProblemError:
    # One answer for unknown, expired, revoked, used and wrong-email tokens: no oracle.
    return ProblemError(
        "This invitation is not valid.",
        status=400,
        title="Bad Request",
        code="invitation_invalid",
    )


async def create_invitation(
    session: AsyncSession, ctx: OrgContext, email: str, role: str, meta: AuditMeta
) -> tuple[Invitation, str]:
    require_user_writer(ctx)
    if not can_grant(ctx.role, role):
        raise ForbiddenError("You can only invite members at or below your own role.")
    token = secrets.token_urlsafe(32)
    inv = Invitation(
        organization_id=ctx.org_id,
        email=email.strip().lower(),
        role=role,
        token_hash=_hash(token),
        invited_by=ctx.principal_id,  # always the caller, never client-supplied (E1-12)
        expires_at=datetime.now(UTC) + INVITATION_TTL,
    )
    session.add(inv)
    await session.flush()
    await reload(session, inv)
    await audit.record(
        session,
        organization_id=ctx.org_id,
        actor_type=ctx.actor_kind,
        actor_id=ctx.principal_id,
        action="invitation.create",
        target_type="invitation",
        target_id=inv.id,
        # audit_log can't be erased, so no plaintext address (ADR-0018).
        metadata={"email_sha256": _hash(inv.email)[:16], "role": role},
        **meta,
    )
    return inv, token


async def list_invitations(session: AsyncSession) -> list[Invitation]:
    return list((await session.scalars(select(Invitation).order_by(Invitation.id.desc()))).all())


async def revoke_invitation(
    session: AsyncSession, ctx: OrgContext, invitation_id: uuid.UUID, meta: AuditMeta
) -> None:
    require_user_writer(ctx)
    inv = await get_in_org(session, Invitation, invitation_id)
    if inv is None:
        raise NotFoundError()
    if inv.accepted_at is not None:
        raise ConflictError("Invitation was already accepted.", code="invitation_accepted")
    if inv.revoked_at is not None:
        return
    inv.revoked_at = datetime.now(UTC)
    await session.flush()
    await audit.record(
        session,
        organization_id=ctx.org_id,
        actor_type=ctx.actor_kind,
        actor_id=ctx.principal_id,
        action="invitation.revoke",
        target_type="invitation",
        target_id=inv.id,
        **meta,
    )


async def accept_invitation(
    session: AsyncSession, principal: Principal, token: str, meta: AuditMeta
) -> Invitation:
    """Consume a token for the calling user.

    The email check compares the invitation to the account email, which is not verified yet
    (email verification is deferred), so the token is the real credential in Phase 1.
    """
    if principal.kind != "user" or principal.api_key is not None or principal.user_id is None:
        raise ForbiddenError("Only users can accept invitations.")
    # The org is only known from the token, so the whole flow runs outside an org context.
    with system_scope():
        inv = await session.scalar(
            select(Invitation).where(Invitation.token_hash == _hash(token)).with_for_update()
        )
        user = await session.get(User, principal.user_id)
        org = await session.get(Organization, inv.organization_id) if inv else None
        if (
            inv is None
            or user is None
            or org is None
            or org.deleted_at is not None
            or inv.accepted_at is not None
            or inv.revoked_at is not None
            or inv.expires_at <= datetime.now(UTC)
            or inv.email != user.email.lower()
        ):
            raise _invalid()
        members = {
            m.principal_id: m
            for m in await session.scalars(
                select(Membership).where(
                    Membership.organization_id == inv.organization_id,
                    Membership.principal_id.in_([inv.invited_by, principal.principal_id]),
                )
            )
        }
        if principal.principal_id in members:
            raise ConflictError("Already a member of this organization.", code="already_member")
        inviter = members.get(inv.invited_by)
        if (
            inviter is None
            or "members:write" not in permissions_for(inviter.role)
            or not can_grant(inviter.role, inv.role)
        ):
            raise ForbiddenError(
                "The inviter can no longer grant this role.", code="inviter_not_authorized"
            )
        inv.accepted_at = datetime.now(UTC)
        session.add(
            Membership(
                organization_id=inv.organization_id,
                principal_id=principal.principal_id,
                role=inv.role,
            )
        )
        await session.flush()
        await audit.record(
            session,
            organization_id=inv.organization_id,
            actor_type="user",
            actor_id=principal.principal_id,
            action="invitation.accept",
            target_type="invitation",
            target_id=inv.id,
            metadata={"role": inv.role, "invited_by": str(inv.invited_by)},
            **meta,
        )
        return inv
