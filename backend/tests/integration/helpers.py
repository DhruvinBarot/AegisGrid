"""Black-box helpers: everything goes through the real HTTP API, nothing is overridden."""

import copy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient, Response

CSRF = {"X-AegisGrid-CSRF": "1"}
PASSWORD = "correct horse battery"

# Smallest valid v1 spec: one LLM call, then end.
SPEC: dict[str, Any] = {
    "schema_version": 1,
    "entry": "answer",
    "nodes": [
        {
            "id": "answer",
            "type": "llm",
            "config": {
                "model": {"provider": "fake", "name": "echo"},
                "prompt": "Answer: {{ input.question }}",
                "output_key": "answer",
            },
        },
        {"id": "done", "type": "end", "config": {"output": "answer"}},
    ],
    "edges": [{"from": "answer", "to": "done"}],
}


def spec(**changes: Any) -> dict[str, Any]:
    """A deep copy of SPEC with top-level keys replaced."""
    return {**copy.deepcopy(SPEC), **changes}


def future(days: int = 30) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


@dataclass
class Actor:
    email: str
    access: str
    refresh: str

    @property
    def h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access}"}


class Api:
    def __init__(self, client: AsyncClient) -> None:
        self.c = client

    async def actor(self, email: str) -> Actor:
        r = await self.c.post(
            "/v1/auth/register",
            json={"email": email, "password": PASSWORD, "display_name": "T"},
        )
        assert r.status_code == 201, r.text
        return await self.login(email)

    async def login(self, email: str, password: str = PASSWORD) -> Actor:
        r = await self.c.post(
            "/v1/auth/login", json={"email": email, "password": password}, headers=CSRF
        )
        assert r.status_code == 200, r.text
        refresh = r.cookies["ag_refresh"]
        self.c.cookies.clear()  # callers send the cookie explicitly
        return Actor(email, r.json()["access_token"], refresh)

    async def org(self, owner: Actor, name: str) -> str:
        r = await self.c.post("/v1/orgs", json={"name": name}, headers=owner.h)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    async def invite(self, org: str, by: Actor, email: str, role: str) -> Response:
        return await self.c.post(
            f"/v1/orgs/{org}/invitations", json={"email": email, "role": role}, headers=by.h
        )

    async def accept(self, who: Actor, token: str) -> Response:
        return await self.c.post("/v1/invitations/accept", json={"token": token}, headers=who.h)

    async def add_member(self, org: str, by: Actor, email: str, role: str) -> Actor:
        who = await self.actor(email)
        r = await self.invite(org, by, email, role)
        assert r.status_code == 201, r.text
        r = await self.accept(who, r.json()["token"])
        assert r.status_code == 200, r.text
        return who

    async def member_id(self, org: str, by: Actor, email: str) -> str:
        r = await self.c.get(f"/v1/orgs/{org}/members", headers=by.h)
        assert r.status_code == 200, r.text
        return next(m["id"] for m in r.json() if m["email"] == email)

    async def env(self, org: str, by: Actor, name: str = "dev") -> str:
        r = await self.c.post(
            f"/v1/orgs/{org}/environments", json={"name": name, "kind": "development"}, headers=by.h
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]

    async def service_account(
        self, org: str, by: Actor, role: str = "operator", name: str = "bot"
    ) -> str:
        r = await self.c.post(
            f"/v1/orgs/{org}/service-accounts", json={"name": name, "role": role}, headers=by.h
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]

    async def key(
        self, org: str, sa: str, by: Actor, scopes: list[str], env: str | None = None
    ) -> dict[str, str]:
        r = await self.c.post(
            f"/v1/orgs/{org}/service-accounts/{sa}/keys",
            json={"scopes": scopes, "expires_at": future(), "environment_id": env},
            headers=by.h,
        )
        assert r.status_code == 201, r.text
        return r.json()

    async def agent(self, org: str, by: Actor, slug: str = "helper") -> str:
        r = await self.c.post(
            f"/v1/orgs/{org}/agents", json={"name": slug.title(), "slug": slug}, headers=by.h
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]

    async def put_draft(self, org: str, agent: str, by: Actor, body: dict[str, Any]) -> Response:
        rev = (await self.c.get(f"/v1/orgs/{org}/agents/{agent}/draft", headers=by.h)).json()
        return await self.c.put(
            f"/v1/orgs/{org}/agents/{agent}/draft",
            json={"spec": body, "draft_rev": rev["draft_rev"]},
            headers=by.h,
        )

    async def publish(
        self, org: str, agent: str, by: Actor, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        r = await self.put_draft(org, agent, by, body or SPEC)
        assert r.status_code == 200, r.text
        r = await self.c.post(f"/v1/orgs/{org}/agents/{agent}/versions", json={}, headers=by.h)
        assert r.status_code == 201, r.text
        return r.json()

    async def deploy(self, org: str, env: str, version: str, by: Actor) -> Response:
        return await self.c.post(
            f"/v1/orgs/{org}/environments/{env}/deployments",
            json={"version_id": version},
            headers=by.h,
        )


@dataclass
class Populated:
    """One org with an extra member, invitation, environment, service account, key, agent,
    published version and deployment."""

    owner: Actor
    org: str
    ids: dict[str, str]  # path-parameter name -> id


async def populate(api: Api, tag: str) -> Populated:
    owner = await api.actor(f"owner-{tag}@example.com")
    org = await api.org(owner, f"Org {tag}")
    await api.add_member(org, owner, f"member-{tag}@example.com", "viewer")
    inv = await api.invite(org, owner, f"pending-{tag}@example.com", "viewer")
    env = await api.env(org, owner)
    sa = await api.service_account(org, owner)
    key = await api.key(org, sa, owner, ["environments:read"])
    agent = await api.agent(org, owner)
    version = (await api.publish(org, agent, owner))["id"]
    deployment = await api.deploy(org, env, version, owner)
    assert deployment.status_code == 201, deployment.text
    return Populated(
        owner,
        org,
        {
            "org": org,
            "member": await api.member_id(org, owner, f"member-{tag}@example.com"),
            "invitation": inv.json()["id"],
            "env": env,
            "sa": sa,
            "key": key["id"],
            "agent": agent,
            "version": version,
            "deployment": deployment.json()["id"],
        },
    )
