"""Service accounts and API keys (ADR-0005/0006)."""

from dataclasses import dataclass

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.helpers import Actor, Api, future


@dataclass
class Setup:
    owner: Actor
    org: str
    sa: str
    key: dict[str, str]  # creation response (id, key_id, key, ...)

    @property
    def raw(self) -> str:
        return self.key["key"]

    @property
    def secret(self) -> str:
        return self.raw.split("_", 3)[3]

    @property
    def h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.raw}"}


@pytest.fixture
async def s(api: Api) -> Setup:
    owner = await api.actor("owner@example.com")
    org = await api.org(owner, "Key Org")
    sa = await api.service_account(org, owner, "operator")
    key = await api.key(org, sa, owner, ["environments:read"])
    return Setup(owner, org, sa, key)


async def test_issued_key_is_shaped_and_works_for_allowed_action(
    client: AsyncClient, s: Setup
) -> None:
    assert s.raw.startswith("agk_live_" + s.key["key_id"] + "_")
    r = await client.get(f"/v1/orgs/{s.org}/environments", headers=s.h)
    assert r.status_code == 200
    # using the key is not an identity: /v1/me is for humans
    assert (await client.get("/v1/me", headers=s.h)).status_code in {401, 403}


async def test_out_of_scope_action_is_403(client: AsyncClient, s: Setup, api: Api) -> None:
    # members:read is a permission of the operator role but not in the key's scopes
    assert (await client.get(f"/v1/orgs/{s.org}/members", headers=s.h)).status_code == 403
    r = await client.post(
        f"/v1/orgs/{s.org}/environments", json={"name": "x", "kind": "development"}, headers=s.h
    )
    assert r.status_code == 403
    assert (await client.get(f"/v1/orgs/{s.org}", headers=s.h)).status_code == 403


async def test_scopes_cannot_exceed_service_account_role_or_issuer(
    client: AsyncClient, s: Setup
) -> None:
    r = await client.post(
        f"/v1/orgs/{s.org}/service-accounts/{s.sa}/keys",
        json={"scopes": ["environments:write"], "expires_at": future()},
        headers=s.owner.h,
    )
    assert r.status_code == 403  # operator role cannot write environments


async def test_key_for_other_org_is_404(client: AsyncClient, s: Setup, api: Api) -> None:
    other_owner = await api.actor("other@example.com")
    other_org = await api.org(other_owner, "Other")
    assert (await client.get(f"/v1/orgs/{other_org}/environments", headers=s.h)).status_code == 404
    assert (await client.get(f"/v1/orgs/{other_org}", headers=s.h)).status_code == 404


async def test_revoked_key_is_401(client: AsyncClient, s: Setup) -> None:
    assert (await client.get(f"/v1/orgs/{s.org}/environments", headers=s.h)).status_code == 200
    r = await client.delete(
        f"/v1/orgs/{s.org}/service-accounts/{s.sa}/keys/{s.key['id']}", headers=s.owner.h
    )
    assert r.status_code == 204
    assert (await client.get(f"/v1/orgs/{s.org}/environments", headers=s.h)).status_code == 401


async def test_deleting_service_account_kills_its_keys(client: AsyncClient, s: Setup) -> None:
    r = await client.delete(f"/v1/orgs/{s.org}/service-accounts/{s.sa}", headers=s.owner.h)
    assert r.status_code == 204
    assert (await client.get(f"/v1/orgs/{s.org}/environments", headers=s.h)).status_code == 401


async def test_expired_key_is_401(client: AsyncClient, s: Setup, db_session: AsyncSession) -> None:
    await db_session.execute(
        text("UPDATE api_keys SET expires_at = now() - interval '1 minute' WHERE key_id = :k"),
        {"k": s.key["key_id"]},
    )
    assert (await client.get(f"/v1/orgs/{s.org}/environments", headers=s.h)).status_code == 401


async def test_wrong_secret_and_malformed_keys_are_401(client: AsyncClient, s: Setup) -> None:
    head, secret = s.raw.rsplit("_", 1)[0], s.secret
    bad = [
        f"{s.raw[:-1]}{'A' if s.raw[-1] != 'A' else 'B'}",
        f"{head.rsplit('_', 1)[0]}_{'x' * len(secret)}",
        f"agk_live_{s.key['key_id']}_",
        f"agk_live_{'0' * 16}_{secret}",  # unknown key id, right-looking secret
        f"agk_prod_{s.key['key_id']}_{secret}",
        "agk_",
        s.secret,
    ]
    for raw in bad:
        r = await client.get(
            f"/v1/orgs/{s.org}/environments", headers={"Authorization": f"Bearer {raw}"}
        )
        assert r.status_code == 401, raw
    # the genuine key is unaffected by the failed guesses
    assert (await client.get(f"/v1/orgs/{s.org}/environments", headers=s.h)).status_code == 200


async def test_keys_cannot_create_keys_or_service_accounts(
    client: AsyncClient, api: Api, s: Setup
) -> None:
    # A key holding apikeys:write still must not manage credentials (humans only).
    sa = await api.service_account(s.org, s.owner, "developer", "bot2")
    k = await api.key(s.org, sa, s.owner, ["apikeys:write", "members:read", "environments:read"])
    h = {"Authorization": f"Bearer {k['key']}"}
    assert (await client.get(f"/v1/orgs/{s.org}/environments", headers=h)).status_code == 200
    r = await client.post(
        f"/v1/orgs/{s.org}/service-accounts", json={"name": "evil", "role": "viewer"}, headers=h
    )
    assert r.status_code == 403
    r = await client.post(
        f"/v1/orgs/{s.org}/service-accounts/{sa}/keys",
        json={"scopes": ["apikeys:write"], "expires_at": future()},
        headers=h,
    )
    assert r.status_code == 403
    r = await client.delete(f"/v1/orgs/{s.org}/service-accounts/{sa}/keys/{k['id']}", headers=h)
    assert r.status_code == 403
    # nor members or invitations
    r = await client.post(
        f"/v1/orgs/{s.org}/invitations",
        json={"email": "z@example.com", "role": "viewer"},
        headers=h,
    )
    assert r.status_code == 403
    assert (await client.get(f"/v1/orgs/{s.org}/service-accounts", headers=h)).status_code == 200


async def test_key_validity_window_is_enforced_at_issue(client: AsyncClient, s: Setup) -> None:
    url = f"/v1/orgs/{s.org}/service-accounts/{s.sa}/keys"
    for expires in (future(-1), future(400), "2030-01-01T00:00:00"):
        r = await client.post(
            url, json={"scopes": ["org:read"], "expires_at": expires}, headers=s.owner.h
        )
        assert r.status_code == 422, expires


async def test_secret_is_never_listed_or_audited(
    client: AsyncClient, s: Setup, db_session: AsyncSession
) -> None:
    listed = await client.get(f"/v1/orgs/{s.org}/service-accounts/{s.sa}/keys", headers=s.owner.h)
    assert listed.status_code == 200
    assert s.secret not in listed.text and s.raw not in listed.text
    assert all(
        "key" not in k and "secret" not in k and "secret_hmac" not in k for k in listed.json()
    )
    sas = await client.get(f"/v1/orgs/{s.org}/service-accounts", headers=s.owner.h)
    assert s.secret not in sas.text
    # use the key, revoke it, then check every place it could have leaked into
    await client.get(f"/v1/orgs/{s.org}/environments", headers=s.h)
    await client.delete(
        f"/v1/orgs/{s.org}/service-accounts/{s.sa}/keys/{s.key['id']}", headers=s.owner.h
    )
    audit = await client.get(f"/v1/orgs/{s.org}/audit-log?limit=100", headers=s.owner.h)
    assert audit.status_code == 200
    actions = {e["action"] for e in audit.json()["items"]}
    assert {"service_account.create", "api_key.create", "api_key.revoke"} <= actions
    assert s.secret not in audit.text and s.raw not in audit.text
    for table in ("audit_log", "api_keys", "service_accounts"):
        n = (
            await db_session.execute(
                text(f"SELECT count(*) FROM {table} t WHERE t::text LIKE :p"),
                {"p": f"%{s.secret}%"},
            )
        ).scalar_one()
        assert n == 0, table


async def test_developer_manages_only_own_service_accounts(api: Api, client: AsyncClient) -> None:
    owner = await api.actor("owner@example.com")
    org = await api.org(owner, "Dev Org")
    dev1 = await api.add_member(org, owner, "dev1@example.com", "developer")
    dev2 = await api.add_member(org, owner, "dev2@example.com", "developer")
    env = await api.env(org, owner)
    sa = await api.service_account(org, dev1, "viewer")
    # developer 2 may not issue keys for developer 1's account
    r = await client.post(
        f"/v1/orgs/{org}/service-accounts/{sa}/keys",
        json={"scopes": ["org:read"], "expires_at": future(), "environment_id": env},
        headers=dev2.h,
    )
    assert r.status_code == 403
    # developer 1 needs a non-protected environment
    r = await client.post(
        f"/v1/orgs/{org}/service-accounts/{sa}/keys",
        json={"scopes": ["org:read"], "expires_at": future()},
        headers=dev1.h,
    )
    assert r.status_code == 403
    r = await client.post(
        f"/v1/orgs/{org}/service-accounts/{sa}/keys",
        json={"scopes": ["org:read"], "expires_at": future(), "environment_id": env},
        headers=dev1.h,
    )
    assert r.status_code == 201
