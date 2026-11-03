from tests.unit.organizations.support import Actor, World, uid


async def setup(w: World) -> tuple[str, Actor, Actor, Actor]:
    owner = await w.user()
    org = await w.make_org(owner)
    return org, owner, await w.member(org, "admin"), await w.member(org, "developer")


async def test_create_and_name_unique(world: World) -> None:
    org, owner, _, dev = await setup(world)
    url = f"/orgs/{org}/environments"
    body = {"name": "qa", "kind": "staging"}
    r = await world.req(owner, "POST", url, json=body)
    assert r.status_code == 201 and r.json()["id"].startswith("env_") and not r.json()["protected"]
    assert (await world.req(owner, "POST", url, json=body)).status_code == 409
    assert (
        await world.req(dev, "POST", url, json={"name": "x", "kind": "development"})
    ).status_code == 403
    assert (await world.req(dev, "GET", url)).status_code == 200


async def test_protected_requires_permission(world: World) -> None:
    org, owner, admin, _ = await setup(world)
    url = f"/orgs/{org}/environments"
    prod = next(e for e in (await world.req(admin, "GET", url)).json() if e["name"] == "production")
    assert prod["protected"] is True
    # Developers cannot write environments at all (403 from environments:write)...
    dev2 = await world.member(org, "developer")
    r = await world.req(dev2, "PATCH", f"{url}/{prod['id']}", json={"settings": {"a": 1}})
    assert r.status_code == 403
    # ... admins can edit protected envs and flip the flag.
    r = await world.req(admin, "PATCH", f"{url}/{prod['id']}", json={"settings": {"a": 1}})
    assert r.status_code == 200 and r.json()["settings"] == {"a": 1}
    r = await world.req(owner, "PATCH", f"{url}/{prod['id']}", json={"protected": False})
    assert r.json()["protected"] is False
    r = await world.req(
        admin, "POST", url, json={"name": "p2", "kind": "production", "protected": True}
    )
    assert r.json()["protected"] is True


async def test_protected_denied_without_permission_in_scope(world: World) -> None:
    org, owner, *_ = await setup(world)
    url = f"/orgs/{org}/environments"
    # A key that may write environments but lacks environments:protected.
    svc = await world.service(uid(org), {"environments:read", "environments:write"}, "admin")
    assert (
        await world.req(svc, "POST", url, json={"name": "k", "kind": "development"})
    ).status_code == 201
    r = await world.req(
        svc, "POST", url, json={"name": "k2", "kind": "development", "protected": True}
    )
    assert r.status_code == 403
    prod = next(e for e in (await world.req(owner, "GET", url)).json() if e["name"] == "production")
    assert (
        await world.req(svc, "PATCH", f"{url}/{prod['id']}", json={"name": "z"})
    ).status_code == 403
    # Unprotected env: protect flag change also needs the permission.
    plain = next(e for e in (await world.req(owner, "GET", url)).json() if e["name"] == "staging")
    assert (
        await world.req(svc, "PATCH", f"{url}/{plain['id']}", json={"protected": True})
    ).status_code == 403
    assert (
        await world.req(svc, "PATCH", f"{url}/{plain['id']}", json={"name": "stg"})
    ).status_code == 200
