import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, LargeBinary, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.ids import new_id

_now = func.now()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("email = lower(email)", name="email_lowercase"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(unique=True)
    display_name: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=_now)
    # Users are pseudonymized, not deleted, while they appear in the audit log (ADR-0018).
    pseudonymized_at: Mapped[datetime | None]


class UserIdentity(Base):
    __tablename__ = "user_identities"
    __table_args__ = (
        UniqueConstraint("provider", "issuer", "subject", name="uq_user_identities_subject"),
        Index("ix_user_identities_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    provider: Mapped[str]
    issuer: Mapped[str]
    subject: Mapped[str]
    email: Mapped[str]
    email_verified: Mapped[bool] = mapped_column(server_default=text("false"))
    # argon2id hash; only set for the password provider.
    password_hash: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=_now)


class Principal(Base):
    __tablename__ = "principals"
    __table_args__ = (
        CheckConstraint("kind IN ('user', 'service')", name="kind"),
        CheckConstraint("(kind = 'user') = (user_id IS NOT NULL)", name="user_iff_user_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    kind: Mapped[str]
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=_now)


class AuthSession(Base):
    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_principal_id", "principal_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(server_default=_now)
    revoked_at: Mapped[datetime | None]
    created_ip: Mapped[str | None]
    user_agent: Mapped[str | None]
    absolute_expires_at: Mapped[datetime]


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_session_id", "session_id"),
        Index("ix_refresh_tokens_family_id", "family_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    family_id: Mapped[uuid.UUID]
    # sha256 hex; the token itself is never stored.
    token_hash: Mapped[str] = mapped_column(unique=True)
    issued_at: Mapped[datetime] = mapped_column(server_default=_now)
    used_at: Mapped[datetime | None]
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]


class SigningKey(Base):
    __tablename__ = "signing_keys"
    __table_args__ = (
        CheckConstraint("status IN ('next', 'active', 'retiring', 'retired')", name="status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    kid: Mapped[str] = mapped_column(unique=True)
    alg: Mapped[str]
    public_jwk: Mapped[dict[str, Any]]
    private_key_pem_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    status: Mapped[str]
    not_before: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=_now)
