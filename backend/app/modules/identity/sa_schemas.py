import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.ids import encode_id
from app.modules.organizations.models import ApiKey, ServiceAccount
from app.modules.organizations.rbac import Role


class ServiceAccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    role: Role


class ServiceAccountOut(BaseModel):
    id: str
    name: str
    role: str
    created_by: str
    created_at: datetime

    @classmethod
    def of(cls, sa: ServiceAccount, role: str) -> "ServiceAccountOut":
        return cls(
            id=encode_id("sa", sa.id),
            name=sa.name,
            role=role,
            created_by=encode_id("prn", sa.created_by),
            created_at=sa.created_at,
        )


class ApiKeyCreate(BaseModel):
    environment_id: str | None = None
    scopes: list[str] = Field(min_length=1, max_length=50)
    expires_at: datetime


class ApiKeyOut(BaseModel):
    """Metadata only: the secret and its HMAC are never returned after creation."""

    id: str
    key_id: str
    service_account_id: str
    environment_id: str | None
    scopes: list[str]
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_by: str
    created_at: datetime

    @classmethod
    def of(cls, k: ApiKey) -> "ApiKeyOut":
        env: uuid.UUID | None = k.environment_id
        return cls(
            id=encode_id("key", k.id),
            key_id=k.key_id,
            service_account_id=encode_id("sa", k.service_account_id),
            environment_id=encode_id("env", env) if env else None,
            scopes=list(k.scopes),
            expires_at=k.expires_at,
            last_used_at=k.last_used_at,
            revoked_at=k.revoked_at,
            created_by=encode_id("prn", k.created_by),
            created_at=k.created_at,
        )


class ApiKeyCreated(ApiKeyOut):
    key: str  # shown exactly once
