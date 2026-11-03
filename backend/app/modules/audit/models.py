import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.ids import new_id


class AuditLog(Base):
    """Append-only (INSERT/SELECT grants plus a trigger).

    Deliberately NOT a TenantMixin: organization_id is nullable (pre-org auth events such as login
    failures) and has no FK so rows outlive org deletion. Readers filter by organization_id
    explicitly; the read endpoint is owner/admin only.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint("outcome IN ('success', 'failure', 'denied')", name="outcome"),
        Index("ix_audit_log_org_occurred_at", "organization_id", text("occurred_at DESC")),
        Index("ix_audit_log_actor", "actor_id", text("occurred_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    organization_id: Mapped[uuid.UUID | None]
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
    actor_type: Mapped[str]
    actor_id: Mapped[uuid.UUID | None]
    ip: Mapped[str | None]
    user_agent: Mapped[str | None]
    action: Mapped[str]
    target_type: Mapped[str | None]
    target_id: Mapped[uuid.UUID | None]
    request_id: Mapped[str | None]
    outcome: Mapped[str] = mapped_column(server_default="success")
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", server_default=text("'{}'::jsonb")
    )
