"""Regressions from the final Phase 1 security review (P1-E2)."""

import threading
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import insert, select, text, union_all
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core import db
from app.core.ids import new_id
from app.core.tenancy import OrgContext, TenantContextError, org_scope, system_scope
from app.modules.environments.models import Environment
from app.modules.identity.providers import password_provider
from app.modules.organizations.models import Organization
from tests.integration.helpers import CSRF, PASSWORD, Api


def _ctx(org_id: uuid.UUID) -> OrgContext:
    return OrgContext(
        org_id=org_id, principal_id=new_id(), role="owner", permissions=frozenset({"*"})
    )


async def _two_orgs(session: AsyncSession) -> tuple[Organization, Organization]:
    a = Organization(slug=f"a-{new_id().hex[:8]}", name="A")
    b = Organization(slug=f"b-{new_id().hex[:8]}", name="B")
    with system_scope():
        session.add_all([a, b])
        await session.flush()
        session.add_all(
            Environment(organization_id=o.id, name="dev", kind="development") for o in (a, b)
        )
        await session.flush()
    return a, b


async def test_statement_insert_into_a_tenant_table_is_refused(db_session: AsyncSession) -> None:
    a, b = await _two_orgs(db_session)
    values = {"id": new_id(), "organization_id": b.id, "name": "x", "kind": "development"}
    with org_scope(_ctx(a.id)), pytest.raises(TenantContextError):
        await db_session.execute(insert(Environment).values(**values))


async def test_insert_from_select_is_refused(db_session: AsyncSession) -> None:
    a, _ = await _two_orgs(db_session)
    source = select(Environment.id, Environment.organization_id, Environment.name)
    stmt = insert(Environment).from_select(["id", "organization_id", "name"], source)
    with org_scope(_ctx(a.id)), pytest.raises(TenantContextError):
        await db_session.execute(stmt)


async def test_column_only_select_needs_a_context_and_is_filtered(
    db_session: AsyncSession,
) -> None:
    a, _ = await _two_orgs(db_session)
    with pytest.raises(TenantContextError):
        await db_session.execute(select(Environment.organization_id))
    with org_scope(_ctx(a.id)):
        rows = await db_session.scalars(select(Environment.organization_id))
        assert set(rows) == {a.id}


async def test_alias_union_and_cte_do_not_leak(db_session: AsyncSession) -> None:
    a, _ = await _two_orgs(db_session)
    alias = aliased(Environment)
    cte = select(Environment.organization_id).cte("c")
    statements = {
        "alias": select(alias.organization_id),
        "union": union_all(select(Environment.organization_id), select(alias.organization_id)),
        "cte": select(cte.c.organization_id),
    }
    with org_scope(_ctx(a.id)):
        for label, stmt in statements.items():
            seen = set((await db_session.execute(stmt)).scalars())
            assert seen <= {a.id}, f"{label} leaked another org's row"


async def test_environment_restricted_key_sees_only_its_environment(api: Api) -> None:
    owner = await api.actor("env-key@example.com")
    org = await api.org(owner, "Env Key")
    env = await api.env(org, owner)
    sa = await api.service_account(org, owner)
    key = await api.key(org, sa, owner, ["environments:read"], env=env)

    r = await api.c.get(
        f"/v1/orgs/{org}/environments", headers={"Authorization": f"Bearer {key['key']}"}
    )
    assert r.status_code == 200, r.text
    assert [e["id"] for e in r.json()] == [env]


@pytest.mark.parametrize("path", ["/v1/auth/logout", "/v1/auth/logout-all"])
async def test_api_keys_cannot_call_session_endpoints(api: Api, path: str) -> None:
    owner = await api.actor(f"logout{path[-3:]}@example.com")
    org = await api.org(owner, "Logout")
    sa = await api.service_account(org, owner)
    key = await api.key(org, sa, owner, ["org:read"])

    r = await api.c.post(path, headers={**CSRF, "Authorization": f"Bearer {key['key']}"})
    assert r.status_code == 403, r.text


async def test_nul_bytes_are_a_client_error(api: Api, client: AsyncClient) -> None:
    r = await client.post(
        "/v1/auth/register",
        json={"email": "nul@example.com", "password": PASSWORD, "display_name": "a\u0000b"},
    )
    assert r.status_code == 422, r.text
    r = await client.post(
        "/v1/auth/login", json={"email": "a\u0000b@example.com", "password": "x"}, headers=CSRF
    )
    assert r.status_code == 422, r.text

    owner = await api.actor("nul-owner@example.com")
    org = await api.org(owner, "Nul")
    r = await client.patch(f"/v1/orgs/{org}", json={"settings": {"a": "b\u0000"}}, headers=owner.h)
    assert r.status_code == 422, r.text


async def test_client_controlled_audit_strings_are_bounded(
    api: Api, db_session: AsyncSession
) -> None:
    owner = await api.actor("bounded@example.com")
    headers = {**owner.h, "User-Agent": "u" * 5000, "X-Request-ID": "r" * 5000}
    r = await api.c.post("/v1/orgs", json={"name": "Bounded"}, headers=headers)
    assert r.status_code == 201, r.text
    row = (
        await db_session.execute(
            text(
                "SELECT length(user_agent), length(request_id) FROM audit_log "
                "WHERE action = 'org.create' ORDER BY occurred_at DESC LIMIT 1"
            )
        )
    ).one()
    assert row[0] <= 512 and row[1] <= 128


async def test_audit_log_keeps_no_plaintext_invitee_email(
    api: Api, db_session: AsyncSession
) -> None:
    owner = await api.actor("pii-owner@example.com")
    org = await api.org(owner, "Pii")
    r = await api.invite(org, owner, "invitee-pii@example.com", "viewer")
    assert r.status_code == 201, r.text
    metadata = (
        await db_session.execute(
            text(
                "SELECT metadata::text FROM audit_log WHERE action = 'invitation.create' "
                "ORDER BY occurred_at DESC LIMIT 1"
            )
        )
    ).scalar_one()
    assert "invitee-pii@example.com" not in metadata


async def test_password_hashing_stays_off_the_event_loop(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: list[threading.Thread] = []
    real = password_provider._hasher

    class Recording:
        def hash(self, password: str) -> str:
            threads.append(threading.current_thread())
            return real.hash(password)

        def verify(self, stored: str, password: str) -> bool:
            threads.append(threading.current_thread())
            return real.verify(stored, password)

    monkeypatch.setattr(password_provider, "_hasher", Recording())
    await api.actor("offloop@example.com")  # registers (hash) then logs in (verify)
    assert len(threads) == 2
    assert threading.main_thread() not in threads


@pytest.mark.parametrize(
    "table",
    [
        "user_identities",
        "sessions",
        "refresh_tokens",
        "invitations",
        "service_accounts",
        "api_keys",
        "environments",
    ],
)
async def test_api_role_cannot_hard_delete_what_it_only_soft_deletes(table: str) -> None:
    async with db.engine.connect() as conn:
        with pytest.raises(ProgrammingError, match="permission denied"):
            await conn.execute(text(f"DELETE FROM {table}"))
        await conn.rollback()
