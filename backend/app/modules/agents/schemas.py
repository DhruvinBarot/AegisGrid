from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.ids import encode_id
from app.modules.agents.models import Agent, AgentDeployment, AgentVersion
from app.modules.agents.spec import Result

SLUG = r"^[a-z0-9][a-z0-9_-]*$"


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None  # pass back as ?cursor= for the next (older) page


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    slug: str = Field(min_length=1, max_length=64, pattern=SLUG)
    description: str = Field(default="", max_length=2000)


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)


class AgentOut(BaseModel):
    id: str
    slug: str
    name: str
    description: str
    draft_rev: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, a: Agent) -> "AgentOut":
        return cls(
            id=encode_id("agt", a.id),
            slug=a.slug,
            name=a.name,
            description=a.description,
            draft_rev=a.draft_rev,
            created_at=a.created_at,
            updated_at=a.updated_at,
        )


class DraftUpdate(BaseModel):
    spec: dict[str, Any]
    draft_rev: int = Field(ge=0, description="The draft_rev you edited; 409 if it moved on.")


class DraftOut(BaseModel):
    agent_id: str
    spec: dict[str, Any] | None
    draft_rev: int
    updated_at: datetime | None

    @classmethod
    def of(cls, a: Agent) -> "DraftOut":
        return cls(
            agent_id=encode_id("agt", a.id),
            spec=a.draft_spec,
            draft_rev=a.draft_rev,
            updated_at=a.draft_updated_at,
        )


class IssueOut(BaseModel):
    path: str
    code: str
    message: str


class ValidationOut(BaseModel):
    valid: bool
    errors: list[IssueOut]
    content_hash: str | None  # what publishing this draft would produce

    @classmethod
    def of(cls, r: Result) -> "ValidationOut":
        return cls(
            valid=r.valid,
            errors=[IssueOut(path=e.path, code=e.code, message=e.message) for e in r.errors],
            content_hash=r.content_hash,
        )


class PublishIn(BaseModel):
    draft_rev: int | None = Field(
        default=None, ge=0, description="If set, publish only if the draft is still at this rev."
    )
    notes: str = Field(default="", max_length=2000)


class VersionOut(BaseModel):
    id: str
    agent_id: str
    version_no: int
    spec_schema_version: int
    content_hash: str
    notes: str
    created_by: str
    created_at: datetime

    @classmethod
    def of(cls, v: AgentVersion) -> "VersionOut":
        return cls(
            id=encode_id("ver", v.id),
            agent_id=encode_id("agt", v.agent_id),
            version_no=v.version_no,
            spec_schema_version=v.spec_schema_version,
            content_hash=v.content_hash,
            notes=v.notes,
            created_by=encode_id("prn", v.created_by),
            created_at=v.created_at,
        )


class VersionDetail(VersionOut):
    spec: dict[str, Any]

    @classmethod
    def of(cls, v: AgentVersion) -> "VersionDetail":
        return cls(**VersionOut.of(v).model_dump(), spec=v.spec)


class DeployIn(BaseModel):
    version_id: str
    notes: str = Field(default="", max_length=2000)


class RollbackIn(BaseModel):
    notes: str = Field(default="", max_length=2000)


class DeploymentOut(BaseModel):
    id: str
    agent_id: str
    environment_id: str
    version_id: str
    version_no: int
    reason: str
    supersedes_id: str | None
    active: bool  # currently the live deployment for its environment
    notes: str
    deployed_by: str
    created_at: datetime

    @classmethod
    def of(cls, d: AgentDeployment, version_no: int, active: bool) -> "DeploymentOut":
        return cls(
            id=encode_id("dep", d.id),
            agent_id=encode_id("agt", d.agent_id),
            environment_id=encode_id("env", d.environment_id),
            version_id=encode_id("ver", d.agent_version_id),
            version_no=version_no,
            reason=d.reason,
            supersedes_id=encode_id("dep", d.supersedes_id) if d.supersedes_id else None,
            active=active,
            notes=d.notes,
            deployed_by=encode_id("prn", d.deployed_by),
            created_at=d.created_at,
        )
