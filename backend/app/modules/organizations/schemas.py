from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.ids import encode_id
from app.modules.identity.models import User
from app.modules.organizations.models import Invitation, Membership, Organization
from app.modules.organizations.rbac import Role

SLUG = r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$"
EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class OrgCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str | None = Field(default=None, pattern=SLUG)


class OrgUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    settings: dict[str, Any] | None = None


class OrgOut(BaseModel):
    id: str
    slug: str
    name: str
    settings: dict[str, Any]
    created_at: datetime

    @classmethod
    def of(cls, o: Organization) -> "OrgOut":
        return cls(
            id=encode_id("org", o.id),
            slug=o.slug,
            name=o.name,
            settings=o.settings,
            created_at=o.created_at,
        )


class MyOrgOut(BaseModel):
    organization: OrgOut
    role: Role


class MemberOut(BaseModel):
    id: str
    email: str
    display_name: str
    role: Role
    created_at: datetime

    @classmethod
    def of(cls, m: Membership, u: User) -> "MemberOut":
        return cls(
            id=encode_id("mem", m.id),
            email=u.email,
            display_name=u.display_name,
            role=m.role,  # pyright: ignore[reportArgumentType]
            created_at=m.created_at,
        )


class MemberUpdate(BaseModel):
    role: Role


class InvitationCreate(BaseModel):
    email: str = Field(max_length=254, pattern=EMAIL)
    role: Role


class InvitationOut(BaseModel):
    id: str
    email: str
    role: Role
    status: str
    expires_at: datetime
    created_at: datetime
    # Only present (set explicitly) when settings.expose_invite_tokens; never on list responses.
    token: str | None = None

    @classmethod
    def of(cls, i: Invitation, now: datetime) -> "InvitationOut":
        if i.accepted_at is not None:
            status = "accepted"
        elif i.revoked_at is not None:
            status = "revoked"
        elif i.expires_at <= now:
            status = "expired"
        else:
            status = "pending"
        return cls(
            id=encode_id("inv", i.id),
            email=i.email,
            role=i.role,  # pyright: ignore[reportArgumentType]
            status=status,
            expires_at=i.expires_at,
            created_at=i.created_at,
        )


class InvitationAccept(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class InvitationAccepted(BaseModel):
    organization_id: str
    role: Role
