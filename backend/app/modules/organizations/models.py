import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.ids import new_id
from app.core.tenancy import TenantMixin

_now = func.now()
_ROLES = "('owner', 'admin', 'developer', 'operator', 'approver', 'viewer')"


class Organization(Base):
    """Tenant root. Not a TenantMixin: it has no organization_id."""

    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    settings: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(server_default=_now)
    updated_at: Mapped[datetime] = mapped_column(server_default=_now, onupdate=_now)
    deleted_at: Mapped[datetime | None]


class Membership(TenantMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_memberships_org_id"),
        UniqueConstraint("organization_id", "principal_id", name="uq_memberships_org_principal"),
        CheckConstraint(f"role IN {_ROLES}", name="role"),
        Index("ix_memberships_principal_id", "principal_id"),
    )

    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    role: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=_now)
    updated_at: Mapped[datetime] = mapped_column(server_default=_now, onupdate=_now)


class Invitation(TenantMixin, Base):
    __tablename__ = "invitations"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_invitations_org_id"),
        CheckConstraint(f"role IN {_ROLES}", name="role"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        Index("ix_invitations_org_email", "organization_id", "email"),
    )

    email: Mapped[str]
    role: Mapped[str]
    token_hash: Mapped[str] = mapped_column(unique=True)
    invited_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=_now)
    expires_at: Mapped[datetime]
    accepted_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


class ServiceAccount(TenantMixin, Base):
    __tablename__ = "service_accounts"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_service_accounts_org_id"),
        UniqueConstraint("organization_id", "name", name="uq_service_accounts_org_name"),
    )

    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"), unique=True)
    name: Mapped[str]
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=_now)


class ApiKey(TenantMixin, Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_api_keys_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "service_account_id"],
            ["service_accounts.organization_id", "service_accounts.id"],
            name="fk_api_keys_service_account",
        ),
        ForeignKeyConstraint(
            ["organization_id", "environment_id"],
            ["environments.organization_id", "environments.id"],
            name="fk_api_keys_environment",
        ),
        Index("ix_api_keys_org_service_account", "organization_id", "service_account_id"),
    )

    service_account_id: Mapped[uuid.UUID]
    environment_id: Mapped[uuid.UUID | None]  # NULL = every environment the scopes allow
    key_id: Mapped[str] = mapped_column(unique=True)
    # HMAC(pepper, secret); the secret itself is never stored.
    secret_hmac: Mapped[bytes] = mapped_column(LargeBinary)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String))
    expires_at: Mapped[datetime]
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=_now)
