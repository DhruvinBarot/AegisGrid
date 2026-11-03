from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_principal
from app.core.tenancy import system_scope
from app.modules.audit.models import AuditLog
from app.modules.organizations.models import ApiKey
from tests.unit.organizations.support import Actor, World, uid


def exp(days: int = 30) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


async def env_id(w: World, owner: Actor, org: str, name: str) -> str:
    envs = (await w.req(owner, "GET", f"/orgs/{org}/environments")).json()
    return next(e["id"] for e in envs if e["name"] == name)


async def make_sa(
    w: World, actor: Actor, org: str, role: str = "operator", name: str = "ci"
) -> dict[str, Any]:
    r = await w.req(
        actor, "POST", f"/orgs/{org}/service-accounts", json={"name": name, "role": role}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def make_key(
    w: World, actor: Actor, org: str, sa: str, scopes: list[str], **extra: Any
) -> Any:
    body = {"scopes": scopes, "expires_at": exp(), **extra}
    return await w.req(actor, "POST", f"/orgs/{org}/service-accounts/{sa}/keys", json=body)


async def test_issue_and_use_key_end_to_end(
    world: World, app: Any, client: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await world.user()
    org = await world.make_org(owner)
    other_org = await world.make_org(await world.user(), "Other")
    sa = await make_sa(world, owner, org)
    dev_env = await env_id(world, owner, org, "development")
    r = await make_key(world, owner, org, sa["id"], ["environments:read"], environment_id=dev_env)
    assert r.status_code == 201, r.text
    created = r.json()
    full = created["key"]
    assert full.startswith("agk_live_") and full.split("_")[2] == created["key_id"]

    listing = (
        await world.req(owner, "GET", f"/orgs/{org}/service-accounts/{sa['id']}/keys")
    ).json()
    assert [k["id"] for k in listing] == [created["id"]]
    assert "key" not in listing[0] and "secret_hmac" not in listing[0]
    assert full.rsplit("_", 1)[-1] not in str(listing)

    # Switch from fake principals to real key authentication.
    app.dependency_overrides.pop(get_current_principal)
    h = {"Authorization": f"Bearer {full}"}
    ok = await client.get(f"/v1/orgs/{org}/environments", headers=h)
    assert ok.status_code == 200
    assert (await client.get(f"/v1/orgs/{other_org}/environments", headers=h)).status_code == 404
    assert (await client.get(f"/v1/orgs/{org}/members", headers=h)).status_code == 403  # no scope
    # A key cannot manage service accounts or keys.
    url = f"/v1/orgs/{org}/service-accounts"
    assert (
        await client.post(url, headers=h, json={"name": "x", "role": "viewer"})
    ).status_code == 403

    # Revoke (as the owner), then the key stops working.
    app.dependency_overrides[get_current_principal] = lambda: owner.principal
    d = await client.delete(f"/v1/orgs/{org}/service-accounts/{sa['id']}/keys/{created['id']}")
    assert d.status_code == 204
    app.dependency_overrides.pop(get_current_principal)
    assert (await client.get(f"/v1/orgs/{org}/environments", headers=h)).status_code == 401

    actions = set(await db_session.scalars(select(AuditLog.action)))
    assert {"service_account.create", "api_key.create", "api_key.revoke"} <= actions


async def test_service_account_role_rules(world: World) -> None:
    owner = await world.user()
    org = await world.make_org(owner)
    admin = await world.member(org, "admin")
    dev = await world.member(org, "developer")
    for role in ("owner", "admin"):
        r = await world.req(
            owner, "POST", f"/orgs/{org}/service-accounts", json={"name": role, "role": role}
        )
        assert r.status_code == 403
    # Role above the caller's rank.
    r = await world.req(
        dev, "POST", f"/orgs/{org}/service-accounts", json={"name": "a", "role": "admin"}
    )
    assert r.status_code == 403
    await make_sa(world, dev, org, "developer", "d1")
    await make_sa(world, admin, org, "developer", "d2")
    assert (
        await world.req(
            owner, "POST", f"/orgs/{org}/service-accounts", json={"name": "d1", "role": "viewer"}
        )
    ).status_code == 409
    viewer = await world.member(org, "viewer")
    r = await world.req(
        viewer, "POST", f"/orgs/{org}/service-accounts", json={"name": "v", "role": "viewer"}
    )
    assert r.status_code == 403
    listed = (await world.req(viewer, "GET", f"/orgs/{org}/service-accounts")).json()
    assert {s["name"] for s in listed} == {"d1", "d2"}


async def test_key_scope_expiry_and_env_rules(world: World) -> None:
    owner = await world.user()
    org = await world.make_org(owner)
    sa = (await make_sa(world, owner, org, "viewer"))["id"]
    # Scope outside the SA role's permissions.
    assert (await make_key(world, owner, org, sa, ["environments:write"])).status_code == 403
    assert (await make_key(world, owner, org, sa, ["environments:read"])).status_code == 201
    # Expiry rules.
    for when in (exp(400), exp(-1)):
        body = {"scopes": ["org:read"], "expires_at": when}
        r = await world.req(owner, "POST", f"/orgs/{org}/service-accounts/{sa}/keys", json=body)
        assert r.status_code == 422
    body = {"scopes": ["org:read"], "expires_at": "2099-01-01T00:00:00"}
    r = await world.req(owner, "POST", f"/orgs/{org}/service-accounts/{sa}/keys", json=body)
    assert r.status_code == 422
    # Environment of another org is not found.
    foreign_org = await world.make_org(await world.user(), "Other")
    foreign_env = await env_id(
        world, await world.member(foreign_org, "owner"), foreign_org, "development"
    )
    r = await make_key(world, owner, org, sa, ["org:read"], environment_id=foreign_env)
    assert r.status_code == 404
    # Created_by is always the caller.
    k = (await make_key(world, owner, org, sa, ["org:read"])).json()
    assert k["created_by"].endswith(owner.principal.principal_id.hex)


async def test_scopes_limited_to_caller_permissions(world: World) -> None:
    owner = await world.user()
    org = await world.make_org(owner)
    dev = await world.member(org, "developer")
    sa = (await make_sa(world, owner, org, "developer"))["id"]
    # Owner-created SA is not the developer's.
    dev_env = await env_id(world, owner, org, "development")
    assert (
        await make_key(world, dev, org, sa, ["org:read"], environment_id=dev_env)
    ).status_code == 403
    mine = (await make_sa(world, dev, org, "developer", "mine"))["id"]
    # Developer rule: needs an environment, and it must not be protected.
    assert (await make_key(world, dev, org, mine, ["org:read"])).status_code == 403
    prod = await env_id(world, owner, org, "production")
    assert (
        await make_key(world, dev, org, mine, ["org:read"], environment_id=prod)
    ).status_code == 403
    assert (
        await make_key(world, dev, org, mine, ["org:read"], environment_id=dev_env)
    ).status_code == 201
    # Developer cannot grant a scope it does not hold.
    assert (
        await make_key(world, dev, org, mine, ["audit:read"], environment_id=dev_env)
    ).status_code == 403


async def test_api_key_cannot_issue_keys_or_delete(world: World) -> None:
    owner = await world.user()
    org = await world.make_org(owner)
    sa = (await make_sa(world, owner, org, "viewer"))["id"]
    svc = await world.service(uid(org), {"apikeys:write", "members:read"}, "developer")
    assert (await make_key(world, svc, org, sa, ["org:read"])).status_code == 403
    assert (await world.req(svc, "DELETE", f"/orgs/{org}/service-accounts/{sa}")).status_code == 403


async def test_delete_service_account_revokes_keys(world: World, db_session: AsyncSession) -> None:
    owner = await world.user()
    org = await world.make_org(owner)
    sa = (await make_sa(world, owner, org, "viewer"))["id"]
    key = (await make_key(world, owner, org, sa, ["org:read"])).json()
    assert (
        await world.req(owner, "DELETE", f"/orgs/{org}/service-accounts/{sa}")
    ).status_code == 204
    assert (await world.req(owner, "GET", f"/orgs/{org}/service-accounts")).json() == []
    assert (
        await world.req(owner, "GET", f"/orgs/{org}/service-accounts/{sa}/keys")
    ).status_code == 404
    with system_scope():
        row = await db_session.scalar(select(ApiKey).where(ApiKey.key_id == key["key_id"]))
    assert row is not None and row.revoked_at is not None


@pytest.mark.parametrize("path", ["/v1/auth/login", "/v1/me", "/.well-known/jwks.json"])
async def test_routes_live_under_v1(client: AsyncClient, path: str) -> None:
    assert (await client.request("GET", path)).status_code != 404
