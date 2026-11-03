import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditLog


async def record(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID | None,
    actor_type: str,
    actor_id: uuid.UUID | None,
    action: str,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    outcome: str = "success",
    metadata: dict[str, Any] | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> None:
    """Insert an audit row in the caller's transaction. Never commits: the event commits or rolls
    back together with the change it describes."""
    session.add(
        AuditLog(
            organization_id=organization_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            outcome=outcome,
            metadata_=metadata or {},
            ip=ip,
            # Client-controlled and reachable unauthenticated; this table can never be pruned.
            user_agent=user_agent[:512] if user_agent else None,
            request_id=request_id[:128] if request_id else None,
        )
    )
    await session.flush()


async def list_for_org(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    limit: int,
    before: tuple[datetime, uuid.UUID] | None = None,
) -> list[AuditLog]:
    """Newest-first page of one org's audit rows. The only sanctioned reader of audit_log.

    audit_log is not a tenant model, so nothing else filters it: this always applies the org.
    `before` is the (occurred_at, id) of the last row of the previous page.
    """
    stmt = select(AuditLog).where(AuditLog.organization_id == organization_id)
    if before is not None:
        stmt = stmt.where(tuple_(AuditLog.occurred_at, AuditLog.id) < tuple_(*before))
    stmt = stmt.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc()).limit(
        max(1, min(limit, 200))
    )
    return list((await session.scalars(stmt)).all())
