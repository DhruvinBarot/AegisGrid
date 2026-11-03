"""revoke DELETE that aegis_api never uses

Only memberships are hard-deleted; everything else is revoked or soft-deleted (P1-E2, E1-11).

Revision ID: 0002
Revises: 0001
"""
from collections.abc import Sequence

from alembic import op

revision: str = '0002'
down_revision: str | Sequence[str] | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "user_identities, sessions, refresh_tokens, invitations, service_accounts, api_keys, "
    "environments"
)


def upgrade() -> None:
    op.execute(f"REVOKE DELETE ON {_TABLES} FROM aegis_api")


def downgrade() -> None:
    op.execute(f"GRANT DELETE ON {_TABLES} TO aegis_api")
