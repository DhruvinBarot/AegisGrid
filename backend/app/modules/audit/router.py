import base64
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.errors import ProblemError
from app.core.tenancy import OrgContext
from app.modules.audit import service
from app.modules.audit.schemas import AuditEntryOut, AuditPage
from app.modules.organizations.deps import require

router = APIRouter(prefix="/v1/orgs/{org}/audit-log")


def _decode(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        at, _, entry_id = raw.partition("|")
        return datetime.fromisoformat(at), uuid.UUID(entry_id)
    except ValueError:
        raise ProblemError(
            "Invalid cursor.", status=422, title="Unprocessable Entity", code="invalid_cursor"
        ) from None


@router.get("", response_model=AuditPage)
async def list_audit_log(
    ctx: Annotated[OrgContext, Depends(require("audit:read"))],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> AuditPage:
    before = _decode(cursor) if cursor else None
    rows = await service.list_for_org(session, ctx.org_id, limit=limit + 1, before=before)
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        raw = f"{page[-1].occurred_at.isoformat()}|{page[-1].id}"
        next_cursor = base64.urlsafe_b64encode(raw.encode()).decode()
    return AuditPage(items=[AuditEntryOut.of(a) for a in page], next_cursor=next_cursor)
