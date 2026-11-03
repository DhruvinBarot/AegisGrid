from app.core.config import settings
from app.core.ids import new_id
from app.modules.organizations import rbac
from tests.unit.organizations.support import World, uid


async def test_create_org_seeds_environments_and_owner(world: World) -> None:
    owner = await world.user()
    org = await world.make_org(owner, "Acme Inc")
    envs = (await world.req(owner, "GET", f"/orgs/{org}/environments")).json()
    assert {e["name"]: e["protected"] for e in envs} == {
        "development": False,
        "staging": False,
        "production": True,
    }
    members = (await world.req(owner, "GET", f"/orgs/{org}/members")).json()
    assert [(m["email"], m["role"]) for m in members] == [(owner.email, "owner")]
    got = (await world.req(owner, "GET", f"/orgs/{org}")).json()
    assert got["slug"].startswith("acme-inc-")


async def test_create_org_slug_conflict_and_api_key_forbidden(world: World) -> None:
    owner = await world.user()
    slug = f"s-{new_id().hex[-8:]}"
    assert (
        await world.req(owner, "POST", "/orgs", json={"name": "A", "slug": slug})
    ).status_code == 201
    dup = await world.req(owner, "POST", "/orgs", json={"name": "B", "slug": slug})
    assert dup.status_code == 409
    org = await world.make_org(owner)
    svc = await world.service(uid(org), {"org:read"})
    assert (await world.req(svc, "POST", "/orgs", json={"name": "X"})).status_code == 403


async def test_me_organizations(world: World) -> None:
    a = await world.user()
    org1 = await world.make_org(a, "One")
    org2 = await world.make_org(a, "Two")
    other = await world.user()
    await world.make_org(other, "Other")
    r = await world.req(a, "GET", "/me/organizations")
    got = {i["organization"]["id"]: i["role"] for i in r.json()}
    assert got == {org1: "owner", org2: "owner"}


async def test_update_and_soft_delete(world: World) -> None:
    owner = await world.user()
    org = await world.make_org(owner)
    admin = await world.member(org, "admin")
    dev = await world.member(org, "developer")
    assert (await world.req(dev, "PATCH", f"/orgs/{org}", json={"name": "N"})).status_code == 403
    r = await world.req(admin, "PATCH", f"/orgs/{org}", json={"name": "Renamed"})
    assert r.json()["name"] == "Renamed"
    assert (await world.req(admin, "DELETE", f"/orgs/{org}")).status_code == 403
    assert (await world.req(owner, "DELETE", f"/orgs/{org}")).status_code == 204
    assert (await world.req(owner, "GET", f"/orgs/{org}")).status_code == 404
    assert (await world.req(owner, "GET", "/me/organizations")).json() == []


async def test_cross_org_and_malformed_ids_are_404(world: World) -> None:
    a, b = await world.user(), await world.user()
    org_a, org_b = await world.make_org(a), await world.make_org(b)
    env_b = (await world.req(b, "GET", f"/orgs/{org_b}/environments")).json()[0]["id"]
    mem_b = (await world.req(b, "GET", f"/orgs/{org_b}/members")).json()[0]["id"]
    inv_b = (
        await world.req(
            b, "POST", f"/orgs/{org_b}/invitations", json={"email": "x@y.zz", "role": "viewer"}
        )
    ).json()["id"]
    probes = [
        ("GET", f"/orgs/{org_b}"),
        ("PATCH", f"/orgs/{org_b}"),
        ("DELETE", f"/orgs/{org_b}"),
        ("GET", f"/orgs/{org_b}/members"),
        ("PATCH", f"/orgs/{org_b}/members/{mem_b}"),
        ("DELETE", f"/orgs/{org_b}/members/{mem_b}"),
        ("GET", f"/orgs/{org_b}/invitations"),
        ("DELETE", f"/orgs/{org_b}/invitations/{inv_b}"),
        ("GET", f"/orgs/{org_b}/environments"),
        ("PATCH", f"/orgs/{org_b}/environments/{env_b}"),
        ("GET", f"/orgs/{org_b}/audit-log"),
        # B's ids under A's org
        ("PATCH", f"/orgs/{org_a}/members/{mem_b}"),
        ("DELETE", f"/orgs/{org_a}/members/{mem_b}"),
        ("DELETE", f"/orgs/{org_a}/invitations/{inv_b}"),
        ("PATCH", f"/orgs/{org_a}/environments/{env_b}"),
        ("GET", "/orgs/org_zzz"),
        ("PATCH", f"/orgs/{org_a}/members/x"),
        ("PATCH", f"/orgs/{org_a}/environments/mem_{'0' * 32}"),
    ]
    for method, url in probes:
        r = await world.req(a, method, url, json={"role": "viewer", "name": "x"})
        assert r.status_code == 404, (method, url, r.status_code)


async def test_member_without_permission_gets_403(world: World) -> None:
    owner = await world.user()
    org = await world.make_org(owner)
    viewer = await world.member(org, "viewer")
    assert (await world.req(viewer, "GET", f"/orgs/{org}/members")).status_code == 200
    assert (await world.req(viewer, "GET", f"/orgs/{org}/audit-log")).status_code == 403
    inv = {"email": "x@y.zz", "role": "viewer"}
    assert (
        await world.req(viewer, "POST", f"/orgs/{org}/invitations", json=inv)
    ).status_code == 403
    assert (await world.req(viewer, "GET", f"/orgs/{org}/invitations")).status_code == 403


async def test_api_key_org_mismatch_and_scope_intersection(world: World) -> None:
    owner = await world.user()
    org = await world.make_org(owner)
    other = await world.make_org(await world.user())
    # Role operator has org:read; the key's scopes decide what survives.
    svc = await world.service(uid(org), {"org:read", "audit:read", "org:write"}, "operator")
    assert (await world.req(svc, "GET", f"/orgs/{org}")).status_code == 200
    assert (await world.req(svc, "GET", f"/orgs/{other}")).status_code == 404
    # audit:read is in scopes but not in the operator role: intersection drops it.
    assert (await world.req(svc, "GET", f"/orgs/{org}/audit-log")).status_code == 403
    assert (await world.req(svc, "PATCH", f"/orgs/{org}", json={"name": "n"})).status_code == 403
    # A role permission missing from the scopes is dropped too.
    narrow = await world.service(uid(org), {"org:read"}, "admin")
    assert (await world.req(narrow, "GET", f"/orgs/{org}/members")).status_code == 403
    assert (await world.req(narrow, "GET", f"/orgs/{org}")).status_code == 200


def test_rbac_map() -> None:
    assert rbac.can_grant("admin", "developer")
    assert not rbac.can_grant("admin", "owner")
    assert rbac.can_grant("owner", "owner")
    assert "org:delete" in rbac.permissions_for("owner")
    assert "org:delete" not in rbac.permissions_for("admin")
    assert "audit:read" not in rbac.permissions_for("developer")
    assert "environments:protected" in rbac.permissions_for("admin")


async def test_expose_invite_tokens_flag(world: World, monkeypatch) -> None:  # noqa: ANN001
    owner = await world.user()
    org = await world.make_org(owner)
    body = {"email": "New@Example.com", "role": "viewer"}
    r = await world.req(owner, "POST", f"/orgs/{org}/invitations", json=body)
    assert len(r.json()["token"]) >= 40 and r.json()["email"] == "new@example.com"
    listed = (await world.req(owner, "GET", f"/orgs/{org}/invitations")).json()
    assert "token" not in listed[0]

    monkeypatch.setattr(settings, "env", "production")
    r2 = await world.req(owner, "POST", f"/orgs/{org}/invitations", json=body)
    assert r2.status_code == 201 and "token" not in r2.json()
