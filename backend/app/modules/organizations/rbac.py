"""Role -> permission map (ADR-0006). Lives in code, versioned with the app.

Roles are totally ordered by `rank`; "grant at or below your own role" and "only an owner can
grant owner" both follow from that order because owner is the single top rank.
"""

from typing import Literal

Role = Literal["owner", "admin", "developer", "operator", "approver", "viewer"]

# Ascending rank.
ROLES: tuple[Role, ...] = ("viewer", "approver", "operator", "developer", "admin", "owner")
_RANK: dict[str, int] = {role: i for i, role in enumerate(ROLES)}

_READ = frozenset({"org:read", "members:read", "environments:read", "agents:read"})
# Build and ship agents (01-tenancy-and-data.md matrix). Deploying to a protected environment
# additionally needs environments:protected, which only owner/admin hold.
_AUTHOR = frozenset({"agents:write", "agents:publish", "agents:deploy"})
_ADMIN = (
    _READ
    | _AUTHOR
    | {
        "org:write",
        "members:write",
        "environments:write",
        "environments:protected",
        "audit:read",
        "apikeys:write",
    }
)

PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": _ADMIN | {"org:delete"},
    "admin": _ADMIN,
    # Developers manage keys only for themselves in non-protected envs; B2 enforces that scope.
    "developer": _READ | _AUTHOR | {"apikeys:write"},
    "operator": _READ,
    "approver": _READ,
    "viewer": _READ,
}


def permissions_for(role: str) -> frozenset[str]:
    return PERMISSIONS.get(role, frozenset())


def rank(role: str) -> int:
    return _RANK[role]


def can_grant(actor_role: str, role: str) -> bool:
    return rank(role) <= rank(actor_role)
