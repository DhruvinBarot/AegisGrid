"""Role and grant rules (ADR-0006)."""

import pytest
from httpx import AsyncClient

from tests.integration.helpers import Actor, Api, future


@pytest.fixture
async def team(api: Api) -> dict[str, Actor | str]:
    owner = await api.actor("owner@example.com")
    org = await api.org(owner, "Team")
    t: dict[str, Actor | str] = {"owner": owner, "org": org}
    for role in ("admin", "developer", "operator", "approver", "viewer"):
        t[role] = await api.add_member(org, owner, f"{role}@example.com", role)
    return t


def actor(t: dict[str, Actor | str], name: str) -> Actor:
    a = t[name]
    assert isinstance(a, Actor)
    return a


async def patch_role(client: AsyncClient, org: str, by: Actor, member_id: str, role: str):
    return await client.patch(
        f"/v1/orgs/{org}/members/{member_id}", json={"role": role}, headers=by.h
    )


async def test_grant_only_at_or_below_own_role(
    client: AsyncClient, api: Api, team: dict[str, Actor | str]
) -> None:
    org = str(team["org"])
    admin, dev = actor(team, "admin"), actor(team, "developer")
    # admin: owner is above, everything else is fine
    assert (await api.invite(org, admin, "n1@example.com", "owner")).status_code == 403
    assert (await api.invite(org, admin, "n2@example.com", "admin")).status_code == 201
    assert (await api.invite(org, admin, "n3@example.com", "viewer")).status_code == 201
    # developer has no members:write at all
    assert (await api.invite(org, dev, "n4@example.com", "viewer")).status_code == 403

    viewer_id = await api.member_id(org, admin, "viewer@example.com")
    assert (await patch_role(client, org, admin, viewer_id, "owner")).status_code == 403
    assert (await patch_role(client, org, admin, viewer_id, "operator")).status_code == 200


async def test_only_owner_grants_owner(
    client: AsyncClient, api: Api, team: dict[str, Actor | str]
) -> None:
    org, owner = str(team["org"]), actor(team, "owner")
    viewer_id = await api.member_id(org, owner, "viewer@example.com")
    assert (await api.invite(org, owner, "own@example.com", "owner")).status_code == 201
    r = await patch_role(client, org, owner, viewer_id, "owner")
    assert r.status_code == 200 and r.json()["role"] == "owner"


async def test_admin_cannot_touch_owner_or_peers_above(
    client: AsyncClient, api: Api, team: dict[str, Actor | str]
) -> None:
    org, owner, admin = str(team["org"]), actor(team, "owner"), actor(team, "admin")
    owner_id = await api.member_id(org, admin, "owner@example.com")
    assert (await patch_role(client, org, admin, owner_id, "viewer")).status_code == 403
    r = await client.delete(f"/v1/orgs/{org}/members/{owner_id}", headers=admin.h)
    assert r.status_code == 403
    assert owner  # owner untouched
    assert any(
        m["role"] == "owner"
        for m in (await client.get(f"/v1/orgs/{org}/members", headers=admin.h)).json()
    )


async def test_nobody_changes_their_own_role(
    client: AsyncClient, api: Api, team: dict[str, Actor | str]
) -> None:
    org = str(team["org"])
    for name in ("owner", "admin"):
        a = actor(team, name)
        own = await api.member_id(org, a, f"{name}@example.com")
        for role in ("viewer", "owner", name):
            assert (await patch_role(client, org, a, own, role)).status_code == 403, (name, role)


async def test_last_owner_cannot_be_removed_or_demoted(
    client: AsyncClient, api: Api, team: dict[str, Actor | str]
) -> None:
    org, owner = str(team["org"]), actor(team, "owner")
    own = await api.member_id(org, owner, "owner@example.com")
    # leaving as the only owner
    r = await client.delete(f"/v1/orgs/{org}/members/{own}", headers=owner.h)
    assert r.status_code == 409
    # demotion (self-change is refused outright; either way the owner stays)
    assert (await patch_role(client, org, owner, own, "admin")).status_code in {403, 409}
    members = (await client.get(f"/v1/orgs/{org}/members", headers=owner.h)).json()
    assert [m["email"] for m in members if m["role"] == "owner"] == ["owner@example.com"]


async def test_second_owner_allows_demotion_and_then_protects_the_remaining_one(
    client: AsyncClient, api: Api, team: dict[str, Actor | str]
) -> None:
    org, owner = str(team["org"]), actor(team, "owner")
    second = await api.add_member(org, owner, "owner2@example.com", "owner")
    second_id = await api.member_id(org, owner, "owner2@example.com")
    own = await api.member_id(org, owner, "owner@example.com")
    assert (await patch_role(client, org, owner, second_id, "admin")).status_code == 200
    # the demoted ex-owner cannot remove or demote the remaining owner
    assert (await patch_role(client, org, second, own, "viewer")).status_code == 403
    assert (
        await client.delete(f"/v1/orgs/{org}/members/{own}", headers=second.h)
    ).status_code == 403
    # and the remaining owner still cannot leave
    assert (
        await client.delete(f"/v1/orgs/{org}/members/{own}", headers=owner.h)
    ).status_code == 409


async def test_non_last_member_can_leave(
    client: AsyncClient, api: Api, team: dict[str, Actor | str]
) -> None:
    org, viewer = str(team["org"]), actor(team, "viewer")
    own = await api.member_id(org, viewer, "viewer@example.com")
    assert (
        await client.delete(f"/v1/orgs/{org}/members/{own}", headers=viewer.h)
    ).status_code == 204
    assert (await client.get(f"/v1/orgs/{org}", headers=viewer.h)).status_code == 404


async def test_viewer_gets_403_on_writes_but_can_read(
    client: AsyncClient, api: Api, team: dict[str, Actor | str]
) -> None:
    org, owner, viewer = str(team["org"]), actor(team, "owner"), actor(team, "viewer")
    env = await api.env(org, owner)
    sa = await api.service_account(org, owner)
    member = await api.member_id(org, owner, "operator@example.com")
    writes = [
        ("PATCH", f"/v1/orgs/{org}", {"name": "x"}),
        ("DELETE", f"/v1/orgs/{org}", None),
        ("POST", f"/v1/orgs/{org}/environments", {"name": "e2", "kind": "development"}),
        ("PATCH", f"/v1/orgs/{org}/environments/{env}", {"name": "e3"}),
        ("POST", f"/v1/orgs/{org}/invitations", {"email": "a@example.com", "role": "viewer"}),
        ("GET", f"/v1/orgs/{org}/invitations", None),
        ("PATCH", f"/v1/orgs/{org}/members/{member}", {"role": "viewer"}),
        ("DELETE", f"/v1/orgs/{org}/members/{member}", None),
        ("POST", f"/v1/orgs/{org}/service-accounts", {"name": "b", "role": "viewer"}),
        ("DELETE", f"/v1/orgs/{org}/service-accounts/{sa}", None),
        (
            "POST",
            f"/v1/orgs/{org}/service-accounts/{sa}/keys",
            {"scopes": ["org:read"], "expires_at": future()},
        ),
        ("GET", f"/v1/orgs/{org}/audit-log", None),
    ]
    for method, url, body in writes:
        r = await client.request(method, url, json=body, headers=viewer.h)
        assert r.status_code == 403, (method, url, r.status_code)
    for url in (f"/v1/orgs/{org}", f"/v1/orgs/{org}/members", f"/v1/orgs/{org}/environments"):
        assert (await client.get(url, headers=viewer.h)).status_code == 200, url


async def test_only_owner_can_delete_org(
    client: AsyncClient, api: Api, team: dict[str, Actor | str]
) -> None:
    org = str(team["org"])
    assert (
        await client.delete(f"/v1/orgs/{org}", headers=actor(team, "admin").h)
    ).status_code == 403
    assert (
        await client.delete(f"/v1/orgs/{org}", headers=actor(team, "owner").h)
    ).status_code == 204
    assert (await client.get(f"/v1/orgs/{org}", headers=actor(team, "owner").h)).status_code == 404


async def test_service_account_role_ceiling(
    client: AsyncClient, api: Api, team: dict[str, Actor | str]
) -> None:
    org, owner = str(team["org"]), actor(team, "owner")
    for role in ("owner", "admin"):
        r = await client.post(
            f"/v1/orgs/{org}/service-accounts", json={"name": role, "role": role}, headers=owner.h
        )
        assert r.status_code == 403, role
    # developer cannot mint a service account above their own role
    dev = actor(team, "developer")
    r = await client.post(
        f"/v1/orgs/{org}/service-accounts", json={"name": "up", "role": "admin"}, headers=dev.h
    )
    assert r.status_code == 403
