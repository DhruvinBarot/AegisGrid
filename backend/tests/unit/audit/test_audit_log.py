from tests.unit.organizations.support import World


async def entries(w: World, actor, org: str, **params) -> list[dict]:  # noqa: ANN001
    r = await w.req(actor, "GET", f"/orgs/{org}/audit-log", params=params)
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def test_mutations_are_audited_and_org_scoped(world: World) -> None:
    a, b = await world.user(), await world.user()
    org_a, org_b = await world.make_org(a), await world.make_org(b)
    await world.req(a, "PATCH", f"/orgs/{org_a}", json={"name": "N"})
    await world.req(
        a, "POST", f"/orgs/{org_a}/environments", json={"name": "qa", "kind": "staging"}
    )
    inv = await world.req(
        a, "POST", f"/orgs/{org_a}/invitations", json={"email": "x@y.zz", "role": "viewer"}
    )
    await world.req(a, "DELETE", f"/orgs/{org_a}/invitations/{inv.json()['id']}")
    guest = await world.user("guest@example.com")
    tok = (
        await world.req(
            a, "POST", f"/orgs/{org_a}/invitations", json={"email": guest.email, "role": "viewer"}
        )
    ).json()["token"]
    await world.req(guest, "POST", "/invitations/accept", json={"token": tok})

    got = await entries(world, a, org_a)
    actions = [e["action"] for e in got]
    assert "org.create" in actions
    for expected in (
        "org.update",
        "environment.create",
        "invitation.create",
        "invitation.revoke",
        "invitation.accept",
    ):
        assert expected in actions
    accept = next(e for e in got if e["action"] == "invitation.accept")
    assert accept["actor_type"] == "user" and accept["actor_id"] == str(
        guest.principal.principal_id
    )
    # Nothing from org B appears, and B sees only its own org.create.
    assert [e["action"] for e in await entries(world, b, org_b)] == ["org.create"]


async def test_cursor_pagination(world: World) -> None:
    a = await world.user()
    org = await world.make_org(a)
    for i in range(4):
        await world.req(a, "PATCH", f"/orgs/{org}", json={"name": f"n{i}"})
    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        r = (await world.req(a, "GET", f"/orgs/{org}/audit-log", params=params)).json()
        seen += [e["id"] for e in r["items"]]
        cursor = r["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 5 and len(set(seen)) == 5  # org.create + 4 updates
    bad = await world.req(a, "GET", f"/orgs/{org}/audit-log", params={"cursor": "garbage"})
    assert bad.status_code == 422


async def test_audit_requires_permission(world: World) -> None:
    a = await world.user()
    org = await world.make_org(a)
    dev = await world.member(org, "developer")
    admin = await world.member(org, "admin")
    assert (await world.req(dev, "GET", f"/orgs/{org}/audit-log")).status_code == 403
    assert (await world.req(admin, "GET", f"/orgs/{org}/audit-log")).status_code == 200
