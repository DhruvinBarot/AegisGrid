"""Test harness for org-scoped routes: fake principals injected through get_current_principal."""

import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import ApiKeyGrant, Principal, get_current_principal
from app.core.ids import new_id
from app.core.tenancy import system_scope
from app.modules.identity.models import Principal as PrincipalRow
from app.modules.identity.models import User
from app.modules.organizations.models import Membership


@dataclass
class Actor:
    principal: Principal
    email: str


class World:
    def __init__(self, app: FastAPI, client: AsyncClient, session: AsyncSession) -> None:
        self.client = client
        self.session = session
        self._current: Principal | None = None
        app.dependency_overrides[get_current_principal] = lambda: self._current

    async def user(self, email: str | None = None) -> Actor:
        email = (email or f"u{new_id().hex[-10:]}@example.com").lower()
        user = User(email=email, display_name=email.split("@")[0])
        self.session.add(user)
        await self.session.flush()
        row = PrincipalRow(kind="user", user_id=user.id)
        self.session.add(row)
        await self.session.flush()
        return Actor(Principal(row.id, "user", user.id, new_id(), None), email)

    async def service(self, org_id: uuid.UUID, scopes: set[str], role: str = "operator") -> Actor:
        """A service principal with a membership and an API-key grant for `org_id`."""
        row = PrincipalRow(kind="service")
        self.session.add(row)
        await self.session.flush()
        await self.add_member(org_id, row.id, role)
        grant = ApiKeyGrant(new_id(), org_id, None, frozenset(scopes))
        return Actor(Principal(row.id, "service", None, None, grant), "")

    async def add_member(self, org_id: uuid.UUID, principal_id: uuid.UUID, role: str) -> None:
        with system_scope():
            self.session.add(
                Membership(organization_id=org_id, principal_id=principal_id, role=role)
            )
            await self.session.flush()

    async def req(self, actor: Actor | None, method: str, url: str, **kw: Any) -> Response:
        self._current = actor.principal if actor else None
        return await self.client.request(method, f"/v1{url}", **kw)

    async def make_org(self, owner: Actor, name: str = "Acme") -> str:
        r = await self.req(owner, "POST", "/orgs", json={"name": name})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    async def member(self, org: str, role: str, email: str | None = None) -> Actor:
        """A user who is a member of `org` with `role` (seeded directly, bypassing invitations)."""
        actor = await self.user(email)
        await self.add_member(uuid.UUID(hex=org[4:]), actor.principal.principal_id, role)
        return actor


def uid(public_id: str) -> uuid.UUID:
    return uuid.UUID(hex=public_id.split("_", 1)[1])
