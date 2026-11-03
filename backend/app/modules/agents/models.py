"""Agents, immutable versions and append-only deployments (ADR-0007).

Every table is org-owned. Cross-table references are composite foreign keys that include
organization_id, so the database itself refuses a row that points into another org; the
deployment keys also pin the version and the state pointer to the same agent and environment.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.tenancy import TenantMixin

_now = func.now()


class Agent(TenantMixin, Base):
    """The stable identity. Holds the single mutable draft (ADR-0007: no separate draft table);
    `draft_rev` is the optimistic lock for draft edits."""

    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_agents_org_id"),
        # Soft-deleted agents free their slug.
        Index(
            "uq_agents_org_slug",
            "organization_id",
            "slug",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        CheckConstraint("draft_rev >= 0", name="draft_rev"),
    )

    slug: Mapped[str]
    name: Mapped[str]
    description: Mapped[str] = mapped_column(server_default="")
    draft_spec: Mapped[dict[str, Any] | None]
    draft_rev: Mapped[int] = mapped_column(server_default=text("0"))
    draft_updated_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=_now)
    updated_at: Mapped[datetime] = mapped_column(server_default=_now, onupdate=_now)
    deleted_at: Mapped[datetime | None]


class AgentVersion(TenantMixin, Base):
    """A published spec. Immutable: aegis_api has no UPDATE/DELETE grant and a trigger rejects
    UPDATE, DELETE and TRUNCATE for every role."""

    __tablename__ = "agent_versions"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_agent_versions_org_id"),
        # Target of the deployments FK: a deployment's version must belong to its agent.
        UniqueConstraint(
            "organization_id", "agent_id", "id", name="uq_agent_versions_org_agent_id"
        ),
        UniqueConstraint("agent_id", "version_no", name="uq_agent_versions_agent_version_no"),
        ForeignKeyConstraint(
            ["organization_id", "agent_id"],
            ["agents.organization_id", "agents.id"],
            name="fk_agent_versions_agent",
        ),
        CheckConstraint("version_no >= 1", name="version_no"),
        CheckConstraint("content_hash ~ '^sha256:[0-9a-f]{64}$'", name="content_hash"),
    )

    agent_id: Mapped[uuid.UUID]
    version_no: Mapped[int]
    spec: Mapped[dict[str, Any]]
    spec_schema_version: Mapped[int]
    content_hash: Mapped[str]
    notes: Mapped[str] = mapped_column(server_default="")
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=_now)


class AgentDeployment(TenantMixin, Base):
    """Append-only history of which version went live in which environment, and why."""

    __tablename__ = "agent_deployments"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_agent_deployments_org_id"),
        UniqueConstraint(
            "organization_id",
            "agent_id",
            "environment_id",
            "id",
            name="uq_agent_deployments_org_agent_env_id",
        ),
        ForeignKeyConstraint(
            ["organization_id", "agent_id", "agent_version_id"],
            ["agent_versions.organization_id", "agent_versions.agent_id", "agent_versions.id"],
            name="fk_agent_deployments_version",
        ),
        ForeignKeyConstraint(
            ["organization_id", "environment_id"],
            ["environments.organization_id", "environments.id"],
            name="fk_agent_deployments_environment",
        ),
        ForeignKeyConstraint(
            ["organization_id", "agent_id", "environment_id", "supersedes_id"],
            [
                "agent_deployments.organization_id",
                "agent_deployments.agent_id",
                "agent_deployments.environment_id",
                "agent_deployments.id",
            ],
            name="fk_agent_deployments_supersedes",
        ),
        CheckConstraint("reason IN ('deploy', 'rollback')", name="reason"),
        Index("ix_agent_deployments_org_agent", "organization_id", "agent_id", "id"),
    )

    agent_id: Mapped[uuid.UUID]
    environment_id: Mapped[uuid.UUID]
    agent_version_id: Mapped[uuid.UUID]
    reason: Mapped[str]
    # The deployment that was active in this environment before this one (None for the first).
    supersedes_id: Mapped[uuid.UUID | None]
    notes: Mapped[str] = mapped_column(server_default="")
    deployed_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=_now)


class AgentEnvironmentState(TenantMixin, Base):
    """The single mutable pointer per (agent, environment) to the active deployment. Moved in
    the same transaction that appends the deployment; its row lock serializes deploys."""

    __tablename__ = "agent_environment_state"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_agent_environment_state_org_id"),
        UniqueConstraint(
            "organization_id",
            "agent_id",
            "environment_id",
            name="uq_agent_environment_state_agent_env",
        ),
        ForeignKeyConstraint(
            ["organization_id", "agent_id", "environment_id", "active_deployment_id"],
            [
                "agent_deployments.organization_id",
                "agent_deployments.agent_id",
                "agent_deployments.environment_id",
                "agent_deployments.id",
            ],
            name="fk_agent_environment_state_deployment",
        ),
    )

    agent_id: Mapped[uuid.UUID]
    environment_id: Mapped[uuid.UUID]
    active_deployment_id: Mapped[uuid.UUID]
    updated_at: Mapped[datetime] = mapped_column(server_default=_now, onupdate=_now)
