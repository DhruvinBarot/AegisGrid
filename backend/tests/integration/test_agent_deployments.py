"""Deployments, rollback, protected environments, RBAC, service accounts and audit (Phase 2)."""

import json

import pytest

from tests.integration.helpers import Actor, Api, future, spec


async def _protected_env(api: Api, org: str, by: Actor, name: str = "prod") -> str:
    r = await api.c.post(
        f"/v1/orgs/{org}/environments",
        json={"name": name, "kind": "production", "protected": True},
        headers=by.h,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
async def team(api: Api) -> dict[str, Actor | str]:
    owner = await api.actor("dep-owner@example.com")
    org = await api.org(owner, "Deployers")
    t: dict[str, Actor | str] = {"owner": owner, "org": org}
    for role in ("admin", "developer", "operator", "approver", "viewer"):
        t[role] = await api.add_member(org, owner, f"dep-{role}@example.com", role)
    t["env"] = await api.env(org, owner)
    t["agent"] = await api.agent(org, owner)
    return t


def a(t: dict[str, Actor | str], name: str) -> Actor:
    x = t[name]
    assert isinstance(x, Actor)
    return x


def s(t: dict[str, Actor | str], name: str) -> str:
    x = t[name]
    assert isinstance(x, str)
    return x


async def test_deploy_rollback_history(api: Api, team: dict[str, Actor | str]) -> None:
    owner, org, env, agent = a(team, "owner"), s(team, "org"), s(team, "env"), s(team, "agent")
    v1 = await api.publish(org, agent, owner)
    v2 = await api.publish(org, agent, owner, spec(limits={"max_steps": 3}))

    d1 = await api.deploy(org, env, v1["id"], owner)
    assert d1.status_code == 201
    first = d1.json()
    assert (first["reason"], first["version_no"], first["supersedes_id"]) == ("deploy", 1, None)
    again = await api.deploy(org, env, v1["id"], owner)
    assert again.status_code == 409 and again.json()["code"] == "already_active"

    second = (await api.deploy(org, env, v2["id"], owner)).json()
    assert second["supersedes_id"] == first["id"]

    r = await api.c.post(
        f"/v1/orgs/{org}/deployments/{first['id']}/rollback", json={}, headers=owner.h
    )
    assert r.status_code == 201
    rolled = r.json()
    assert (rolled["reason"], rolled["version_no"]) == ("rollback", 1)
    assert rolled["supersedes_id"] == second["id"] and rolled["id"] != first["id"]

    noop = await api.c.post(
        f"/v1/orgs/{org}/deployments/{first['id']}/rollback", json={}, headers=owner.h
    )
    assert noop.status_code == 409  # v1 is already live again

    history = (
        await api.c.get(f"/v1/orgs/{org}/agents/{agent}/deployments", headers=owner.h)
    ).json()["items"]
    assert [d["id"] for d in history] == [rolled["id"], second["id"], first["id"]]
    assert [d["active"] for d in history] == [True, False, False]
    # Earlier rows are untouched: rollback appended instead of rewriting.
    assert {k: v for k, v in history[2].items() if k != "active"} == {
        k: v for k, v in first.items() if k != "active"
    }

    other_env = await api.env(org, owner, "qa")
    filtered = await api.c.get(
        f"/v1/orgs/{org}/agents/{agent}/deployments",
        params={"environment_id": other_env},
        headers=owner.h,
    )
    assert filtered.json()["items"] == []
    paged = await api.c.get(
        f"/v1/orgs/{org}/agents/{agent}/deployments", params={"limit": 2}, headers=owner.h
    )
    assert len(paged.json()["items"]) == 2 and paged.json()["next_cursor"]


async def test_protected_environment_needs_admin(api: Api, team: dict[str, Actor | str]) -> None:
    owner, org, agent = a(team, "owner"), s(team, "org"), s(team, "agent")
    prod = await _protected_env(api, org, owner)
    v1 = await api.publish(org, agent, owner)

    r = await api.deploy(org, prod, v1["id"], a(team, "developer"))
    assert r.status_code == 403 and r.json()["code"] == "protected_environment"
    assert (
        await api.deploy(org, s(team, "env"), v1["id"], a(team, "developer"))
    ).status_code == 201
    first = await api.deploy(org, prod, v1["id"], a(team, "admin"))
    assert first.status_code == 201

    v2 = await api.publish(org, agent, owner, spec(limits={"max_steps": 2}))
    assert (await api.deploy(org, prod, v2["id"], owner)).status_code == 201
    rb = f"/v1/orgs/{org}/deployments/{first.json()['id']}/rollback"
    assert (await api.c.post(rb, json={}, headers=a(team, "developer").h)).status_code == 403
    assert (await api.c.post(rb, json={}, headers=a(team, "admin").h)).status_code == 201


@pytest.mark.parametrize("role", ["operator", "approver", "viewer"])
async def test_read_only_roles_cannot_change_agents(
    api: Api, team: dict[str, Actor | str], role: str
) -> None:
    owner, org, env, agent = a(team, "owner"), s(team, "org"), s(team, "env"), s(team, "agent")
    v1 = await api.publish(org, agent, owner)
    dep = (await api.deploy(org, env, v1["id"], owner)).json()
    who = a(team, role).h
    base = f"/v1/orgs/{org}"
    attempts = [
        ("POST", f"{base}/agents", {"name": "X", "slug": "x"}),
        ("PATCH", f"{base}/agents/{agent}", {"name": "X"}),
        ("PUT", f"{base}/agents/{agent}/draft", {"spec": {}, "draft_rev": 1}),
        ("POST", f"{base}/agents/{agent}/versions", {}),
        ("POST", f"{base}/environments/{env}/deployments", {"version_id": v1["id"]}),
        ("POST", f"{base}/deployments/{dep['id']}/rollback", {}),
    ]
    for method, url, body in attempts:
        r = await api.c.request(method, url, json=body, headers=who)
        assert r.status_code == 403, f"{role} {method} {url} -> {r.status_code}"
    for url in (
        f"{base}/agents",
        f"{base}/agents/{agent}",
        f"{base}/agents/{agent}/draft",
        f"{base}/agents/{agent}/versions/{v1['id']}",
        f"{base}/agents/{agent}/deployments",
    ):
        assert (await api.c.get(url, headers=who)).status_code == 200, url
    r = await api.c.post(f"{base}/agents/{agent}/draft/validate", headers=who)
    assert r.status_code == 200  # a dry run, no write


async def test_developer_builds_and_ships(api: Api, team: dict[str, Actor | str]) -> None:
    dev, org, env = a(team, "developer"), s(team, "org"), s(team, "env")
    agent = await api.agent(org, dev, "devs-agent")
    v1 = await api.publish(org, agent, dev)
    assert (await api.deploy(org, env, v1["id"], dev)).status_code == 201


async def test_cannot_deploy_another_orgs_version(api: Api, team: dict[str, Actor | str]) -> None:
    owner, org, env = a(team, "owner"), s(team, "org"), s(team, "env")
    stranger = await api.actor("stranger@example.com")
    other_org = await api.org(stranger, "Other")
    foreign = await api.publish(other_org, await api.agent(other_org, stranger), stranger)
    r = await api.deploy(org, env, foreign["id"], owner)
    assert r.status_code == 404
    malformed = await api.deploy(org, env, "not-an-id", owner)
    assert malformed.status_code == 422 and malformed.json()["code"] == "invalid_version_id"


async def test_service_account_keys_are_bounded_by_scopes_and_environment(
    api: Api, team: dict[str, Actor | str]
) -> None:
    owner, org, env, agent = a(team, "owner"), s(team, "org"), s(team, "env"), s(team, "agent")
    other_env = await api.env(org, owner, "qa")
    v1 = await api.publish(org, agent, owner)
    sa = await api.service_account(org, owner, role="developer", name="ci")

    reader = await api.key(org, sa, owner, ["agents:read"])
    h = {"Authorization": f"Bearer {reader['key']}"}
    assert (await api.c.get(f"/v1/orgs/{org}/agents/{agent}", headers=h)).status_code == 200
    r = await api.c.post(f"/v1/orgs/{org}/agents", json={"name": "N", "slug": "n"}, headers=h)
    assert r.status_code == 403
    assert (
        await api.c.post(
            f"/v1/orgs/{org}/environments/{env}/deployments",
            json={"version_id": v1["id"]},
            headers=h,
        )
    ).status_code == 403

    shipper = await api.key(org, sa, owner, ["agents:read", "agents:deploy"], env=env)
    h = {"Authorization": f"Bearer {shipper['key']}"}
    url = f"/v1/orgs/{org}/environments/{{}}/deployments"
    r = await api.c.post(url.format(other_env), json={"version_id": v1["id"]}, headers=h)
    assert r.status_code == 404  # an env-restricted key cannot see other environments
    r = await api.c.post(url.format(env), json={"version_id": v1["id"]}, headers=h)
    assert r.status_code == 201

    operator_sa = await api.service_account(org, owner, role="operator", name="runner")
    r = await api.c.post(
        f"/v1/orgs/{org}/service-accounts/{operator_sa}/keys",
        json={"scopes": ["agents:write"], "expires_at": future()},
        headers=owner.h,
    )
    assert r.status_code == 403  # scopes can never exceed the service account's role


async def test_every_change_is_audited_without_spec_content(
    api: Api, team: dict[str, Actor | str]
) -> None:
    owner, org, env, agent = a(team, "owner"), s(team, "org"), s(team, "env"), s(team, "agent")
    await api.c.patch(f"/v1/orgs/{org}/agents/{agent}", json={"name": "N"}, headers=owner.h)
    v1 = await api.publish(org, agent, owner)
    v2 = await api.publish(org, agent, owner, spec(limits={"max_steps": 4}))
    first = (await api.deploy(org, env, v1["id"], owner)).json()
    await api.deploy(org, env, v2["id"], owner)
    await api.c.post(f"/v1/orgs/{org}/deployments/{first['id']}/rollback", json={}, headers=owner.h)

    items = (
        await api.c.get(f"/v1/orgs/{org}/audit-log", params={"limit": 100}, headers=owner.h)
    ).json()["items"]
    actions = [i["action"] for i in items if i["action"].startswith("agent.")]
    assert set(actions) == {
        "agent.create",
        "agent.update",
        "agent.draft_update",
        "agent.publish",
        "agent.deploy",
        "agent.rollback",
    }
    publish = next(i for i in items if i["action"] == "agent.publish")
    assert publish["metadata"]["content_hash"].startswith("sha256:")
    dumped = json.dumps([i["metadata"] for i in items])
    assert "Answer:" not in dumped and "prompt" not in dumped  # no spec content in audit
