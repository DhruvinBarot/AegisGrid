from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.tenancy import TenantMixin

_now = func.now()


class Environment(TenantMixin, Base):
    __tablename__ = "environments"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_environments_org_id"),
        CheckConstraint("kind IN ('development', 'staging', 'production')", name="kind"),
        # Soft-deleted environments free their name.
        Index(
            "uq_environments_org_name",
            "organization_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    name: Mapped[str]
    kind: Mapped[str]
    protected: Mapped[bool] = mapped_column(server_default=text("false"))
    settings: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(server_default=_now)
    updated_at: Mapped[datetime] = mapped_column(server_default=_now, onupdate=_now)
    deleted_at: Mapped[datetime | None]
