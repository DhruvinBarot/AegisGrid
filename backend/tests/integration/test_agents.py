"""Agents, drafts, validation and publishing through the HTTP API (Phase 2)."""

import pytest

from tests.integration.helpers import SPEC, Actor, Api, spec


@pytest.fixture
async def org(api: Api) -> tuple[Actor, str]:
    owner = await api.actor("agents-owner@example.com")
    return owner, await api.org(owner, "Agents")


async def test_agent_crud_and_pagination(api: Api, org: tuple[Actor, str]) -> None:
    owner, o = org
    base = f"/v1/orgs/{o}/agents"
    created = [await api.agent(o, owner, slug=f"a{i}") for i in range(3)]

    r = await api.c.post(base, json={"name": "Dup", "slug": "a0"}, headers=owner.h)
    assert r.status_code == 409 and r.json()["code"] == "agent_slug_taken"
    r = await api.c.post(base, json={"name": "Bad", "slug": "Not A Slug"}, headers=owner.h)
    assert r.status_code == 422

    first = (await api.c.get(base, params={"limit": 2}, headers=owner.h)).json()
    assert [a["id"] for a in first["items"]] == created[:0:-1]  # newest first
    rest = await api.c.get(
        base, params={"limit": 2, "cursor": first["next_cursor"]}, headers=owner.h
    )
    assert [a["id"] for a in rest.json()["items"]] == [created[0]]
    assert rest.json()["next_cursor"] is None
    bad = await api.c.get(base, params={"cursor": "garbage"}, headers=owner.h)
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_cursor"

    r = await api.c.patch(
        f"{base}/{created[0]}", json={"name": "Renamed", "description": "d"}, headers=owner.h
    )
    assert r.status_code == 200
    got = (await api.c.get(f"{base}/{created[0]}", headers=owner.h)).json()
    assert (got["name"], got["description"], got["slug"]) == ("Renamed", "d", "a0")


async def test_draft_optimistic_locking(api: Api, org: tuple[Actor, str]) -> None:
    owner, o = org
    agent = await api.agent(o, owner)
    url = f"/v1/orgs/{o}/agents/{agent}/draft"
    empty = (await api.c.get(url, headers=owner.h)).json()
    assert (empty["spec"], empty["draft_rev"]) == (None, 0)

    r = await api.c.put(url, json={"spec": SPEC, "draft_rev": 0}, headers=owner.h)
    assert r.status_code == 200 and r.json()["draft_rev"] == 1
    stale = await api.c.put(url, json={"spec": {}, "draft_rev": 0}, headers=owner.h)
    assert stale.status_code == 409
    assert (stale.json()["code"], stale.json()["draft_rev"]) == ("draft_conflict", 1)
    assert (await api.c.get(url, headers=owner.h)).json()["spec"] == SPEC


async def test_validate_reports_errors_without_writing(api: Api, org: tuple[Actor, str]) -> None:
    owner, o = org
    agent = await api.agent(o, owner)
    url = f"/v1/orgs/{o}/agents/{agent}/draft/validate"
    r = (await api.c.post(url, headers=owner.h)).json()
    assert r["valid"] is False and r["errors"][0]["code"] == "empty_draft"

    # A dangling edge: stored as a draft (drafts are work in progress), reported by validate.
    broken = spec(edges=[{"from": "answer", "to": "nowhere"}])
    assert (await api.put_draft(o, agent, owner, broken)).status_code == 200
    r = (await api.c.post(url, headers=owner.h)).json()
    assert r["valid"] is False and r["content_hash"] is None
    assert {(e["path"], e["code"]) for e in r["errors"]} >= {("edges[0].to", "unknown_node")}

    assert (await api.put_draft(o, agent, owner, SPEC)).status_code == 200
    r = (await api.c.post(url, headers=owner.h)).json()
    assert r["valid"] is True and r["errors"] == []
    assert r["content_hash"].startswith("sha256:")
    versions = await api.c.get(f"/v1/orgs/{o}/agents/{agent}/versions", headers=owner.h)
    assert versions.json()["items"] == []  # validate never publishes


async def test_publish_rejects_invalid_spec_with_structured_errors(
    api: Api, org: tuple[Actor, str]
) -> None:
    owner, o = org
    agent = await api.agent(o, owner)
    bad = spec(nodes=[{"id": "x", "type": "teleport"}])
    assert (await api.put_draft(o, agent, owner, bad)).status_code == 200
    r = await api.c.post(f"/v1/orgs/{o}/agents/{agent}/versions", json={}, headers=owner.h)
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "invalid_spec"
    assert r.headers["content-type"].startswith("application/problem+json")
    assert body["errors"][0]["path"] == "nodes[0]"
    assert body["errors"][0]["code"] == "union_tag_invalid"


async def test_publish_numbers_versions_and_hash_is_deterministic(
    api: Api, org: tuple[Actor, str]
) -> None:
    owner, o = org
    agent = await api.agent(o, owner)
    v1 = await api.publish(o, agent, owner)
    # Same content, different key order and explicit defaults: same canonical hash.
    reordered = dict(reversed(list(spec(limits={"max_steps": 25}).items())))
    v2 = await api.publish(o, agent, owner, reordered)
    v3 = await api.publish(o, agent, owner, spec(limits={"max_steps": 5}))
    assert [v["version_no"] for v in (v1, v2, v3)] == [1, 2, 3]
    assert v1["content_hash"] == v2["content_hash"] != v3["content_hash"]
    assert v1["spec_schema_version"] == 1

    listed = (await api.c.get(f"/v1/orgs/{o}/agents/{agent}/versions", headers=owner.h)).json()
    assert [v["version_no"] for v in listed["items"]] == [3, 2, 1]
    detail = (
        await api.c.get(f"/v1/orgs/{o}/agents/{agent}/versions/{v1['id']}", headers=owner.h)
    ).json()
    assert detail["spec"]["limits"] == {"max_steps": 25}  # stored normalized, defaults filled
    assert detail["spec"]["edges"] == [{"from": "answer", "to": "done", "when": None}]


async def test_publish_honours_expected_draft_rev(api: Api, org: tuple[Actor, str]) -> None:
    owner, o = org
    agent = await api.agent(o, owner)
    assert (await api.put_draft(o, agent, owner, SPEC)).status_code == 200
    url = f"/v1/orgs/{o}/agents/{agent}/versions"
    r = await api.c.post(url, json={"draft_rev": 0}, headers=owner.h)
    assert r.status_code == 409 and r.json()["code"] == "draft_conflict"
    r = await api.c.post(url, json={"draft_rev": 1, "notes": "first"}, headers=owner.h)
    assert r.status_code == 201 and r.json()["notes"] == "first"


async def test_versions_have_no_write_routes(api: Api, org: tuple[Actor, str]) -> None:
    owner, o = org
    agent = await api.agent(o, owner)
    v = await api.publish(o, agent, owner)
    url = f"/v1/orgs/{o}/agents/{agent}/versions/{v['id']}"
    for method in ("PUT", "PATCH", "DELETE"):
        r = await api.c.request(method, url, json={}, headers=owner.h)
        assert r.status_code == 405, method


async def test_version_must_belong_to_the_addressed_agent(api: Api, org: tuple[Actor, str]) -> None:
    owner, o = org
    a, b = await api.agent(o, owner, "first"), await api.agent(o, owner, "second")
    v = await api.publish(o, a, owner)
    r = await api.c.get(f"/v1/orgs/{o}/agents/{b}/versions/{v['id']}", headers=owner.h)
    assert r.status_code == 404
