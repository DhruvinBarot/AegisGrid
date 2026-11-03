from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.core.ids import encode_id
from app.modules.audit.models import AuditLog


class AuditEntryOut(BaseModel):
    id: str
    occurred_at: datetime
    actor_type: str
    actor_id: str | None
    action: str
    target_type: str | None
    target_id: str | None
    outcome: str
    ip: str | None
    request_id: str | None
    metadata: dict[str, Any]

    @classmethod
    def of(cls, a: AuditLog) -> "AuditEntryOut":
        return cls(
            id=encode_id("aud", a.id),
            occurred_at=a.occurred_at,
            actor_type=a.actor_type,
            actor_id=str(a.actor_id) if a.actor_id else None,
            action=a.action,
            target_type=a.target_type,
            target_id=str(a.target_id) if a.target_id else None,
            outcome=a.outcome,
            ip=a.ip,
            request_id=a.request_id,
            metadata=a.metadata_,
        )


class AuditPage(BaseModel):
    items: list[AuditEntryOut]
    next_cursor: str | None
