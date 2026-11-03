"""agents, immutable versions, append-only deployments (Phase 2, ADR-0007)

Additive only. agent_versions and agent_deployments are immutable twice over: aegis_api gets
SELECT and INSERT only, and a trigger rejects UPDATE, DELETE and TRUNCATE for every role.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IMMUTABLE = ("agent_versions", "agent_deployments")


def _ts(name: str, nullable: bool = False, default: bool = True) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.text("now()") if default else None,
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), server_default="", nullable=False),
        sa.Column("draft_spec", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("draft_rev", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _ts("draft_updated_at", nullable=True, default=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("deleted_at", nullable=True, default=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("draft_rev >= 0", name=op.f("ck_agents_draft_rev")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["principals.id"], name=op.f("fk_agents_created_by")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], name=op.f("fk_agents_organization_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agents")),
        sa.UniqueConstraint("organization_id", "id", name="uq_agents_org_id"),
    )
    op.create_index(
        "uq_agents_org_slug",
        "agents",
        ["organization_id", "slug"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "agent_versions",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("spec_schema_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("notes", sa.String(), server_default="", nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        _ts("created_at"),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "content_hash ~ '^sha256:[0-9a-f]{64}$'", name=op.f("ck_agent_versions_content_hash")
        ),
        sa.CheckConstraint("version_no >= 1", name=op.f("ck_agent_versions_version_no")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["principals.id"], name=op.f("fk_agent_versions_created_by")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "agent_id"],
            ["agents.organization_id", "agents.id"],
            name="fk_agent_versions_agent",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_agent_versions_organization_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_versions")),
        sa.UniqueConstraint("agent_id", "version_no", name="uq_agent_versions_agent_version_no"),
        sa.UniqueConstraint(
            "organization_id", "agent_id", "id", name="uq_agent_versions_org_agent_id"
        ),
        sa.UniqueConstraint("organization_id", "id", name="uq_agent_versions_org_id"),
    )

    op.create_table(
        "agent_deployments",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("notes", sa.String(), server_default="", nullable=False),
        sa.Column("deployed_by", sa.Uuid(), nullable=False),
        _ts("created_at"),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "reason IN ('deploy', 'rollback')", name=op.f("ck_agent_deployments_reason")
        ),
        sa.ForeignKeyConstraint(
            ["deployed_by"], ["principals.id"], name=op.f("fk_agent_deployments_deployed_by")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "agent_id", "agent_version_id"],
            ["agent_versions.organization_id", "agent_versions.agent_id", "agent_versions.id"],
            name="fk_agent_deployments_version",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "environment_id"],
            ["environments.organization_id", "environments.id"],
            name="fk_agent_deployments_environment",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "agent_id", "environment_id", "supersedes_id"],
            [
                "agent_deployments.organization_id",
                "agent_deployments.agent_id",
                "agent_deployments.environment_id",
                "agent_deployments.id",
            ],
            name="fk_agent_deployments_supersedes",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_agent_deployments_organization_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_deployments")),
        sa.UniqueConstraint(
            "organization_id",
            "agent_id",
            "environment_id",
            "id",
            name="uq_agent_deployments_org_agent_env_id",
        ),
        sa.UniqueConstraint("organization_id", "id", name="uq_agent_deployments_org_id"),
    )
    op.create_index(
        "ix_agent_deployments_org_agent",
        "agent_deployments",
        ["organization_id", "agent_id", "id"],
        unique=False,
    )

    op.create_table(
        "agent_environment_state",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("active_deployment_id", sa.Uuid(), nullable=False),
        _ts("updated_at"),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "agent_id", "environment_id", "active_deployment_id"],
            [
                "agent_deployments.organization_id",
                "agent_deployments.agent_id",
                "agent_deployments.environment_id",
                "agent_deployments.id",
            ],
            name="fk_agent_environment_state_deployment",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_agent_environment_state_organization_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_environment_state")),
        sa.UniqueConstraint(
            "organization_id",
            "agent_id",
            "environment_id",
            name="uq_agent_environment_state_agent_env",
        ),
        sa.UniqueConstraint("organization_id", "id", name="uq_agent_environment_state_org_id"),
    )

    # Same shape as audit_log's trigger, but names the table so one function serves both.
    op.execute("""
        CREATE FUNCTION reject_immutable_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is immutable (% rejected)', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END $$
    """)
    op.execute("REVOKE EXECUTE ON FUNCTION reject_immutable_mutation() FROM PUBLIC")
    for table in _IMMUTABLE:
        op.execute(f"""
            CREATE TRIGGER {table}_no_update_delete BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation()
        """)
        op.execute(f"""
            CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION reject_immutable_mutation()
        """)

    # Grants (ADR-0004). Agents are soft-deleted (no DELETE); history tables are insert-only.
    op.execute("GRANT SELECT, INSERT, UPDATE ON agents, agent_environment_state TO aegis_api")
    op.execute("GRANT SELECT, INSERT ON agent_versions, agent_deployments TO aegis_api")


def downgrade() -> None:
    op.drop_table("agent_environment_state")
    op.drop_index("ix_agent_deployments_org_agent", table_name="agent_deployments")
    op.drop_table("agent_deployments")
    op.drop_table("agent_versions")
    op.drop_index(
        "uq_agents_org_slug", table_name="agents", postgresql_where=sa.text("deleted_at IS NULL")
    )
    op.drop_table("agents")
    op.execute("DROP FUNCTION reject_immutable_mutation()")
