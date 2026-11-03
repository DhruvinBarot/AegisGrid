"""Invitation lifecycle (ADR-0006)."""

import hashlib
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from tests.integration.helpers import Actor, Api


@pytest.fixture
async def org(api: Api) -> tuple[Actor, str]:
    owner = await api.actor("owner@example.com")
    return owner, await api.org(owner, "Inv Org")


async def invite_token(api: Api, org: str, by: Actor, email: str, role: str = "viewer") -> dict:
    r = await api.invite(org, by, email, role)
    assert r.status_code == 201, r.text
    return r.json()


async def test_single_use(api: Api, org: tuple[Actor, str]) -> None:
    owner, oid = org
    inv = await invite_token(api, oid, owner, "invitee@example.com", "developer")
    assert inv["status"] == "pending"
    invitee = await api.actor("invitee@example.com")
    r = await api.accept(invitee, inv["token"])
    assert r.status_code == 200
    assert r.json() == {"organization_id": oid, "role": "developer"}
    # same user again, then another user with the same token
    assert (await api.accept(invitee, inv["token"])).status_code == 400
    other = await api.actor("other@example.com")
    assert (await api.accept(other, inv["token"])).status_code == 400
    # invitee is now a member with the invited role
    mine = (await api.c.get("/v1/me/organizations", headers=invitee.h)).json()
    assert [(o["organization"]["id"], o["role"]) for o in mine] == [(oid, "developer")]


async def test_unknown_token_wrong_email_and_unauthenticated(
    client: AsyncClient, api: Api, org: tuple[Actor, str]
) -> None:
    owner, oid = org
    inv = await invite_token(api, oid, owner, "right@example.com")
    wrong = await api.actor("wrong@example.com")
    r = await api.accept(wrong, inv["token"])
    assert r.status_code == 400
    # a failed wrong-email attempt does not burn the invitation
    right = await api.actor("right@example.com")
    assert (await api.accept(right, inv["token"])).status_code == 200
    assert (await api.accept(right, "no-such-token")).status_code == 400
    r = await client.post("/v1/invitations/accept", json={"token": inv["token"]})
    assert r.status_code == 401


async def test_identical_error_for_every_invalid_token(api: Api, org: tuple[Actor, str]) -> None:
    owner, oid = org
    inv = await invite_token(api, oid, owner, "a@example.com")
    stranger = await api.actor("stranger@example.com")
    unknown = await api.accept(stranger, "nope")
    wrong_email = await api.accept(stranger, inv["token"])
    strip = lambda r: {k: v for k, v in r.json().items() if k != "instance"}  # noqa: E731
    assert unknown.status_code == wrong_email.status_code == 400
    assert strip(unknown) == strip(wrong_email)


async def test_expired_invitation_rejected(
    api: Api, org: tuple[Actor, str], db_session: AsyncSession
) -> None:
    owner, oid = org
    inv = await invite_token(api, oid, owner, "late@example.com")
    invitee = await api.actor("late@example.com")
    inv_pk = uuid.UUID(hex=inv["id"].removeprefix("inv_"))
    await db_session.execute(
        text("UPDATE invitations SET expires_at = now() - interval '1 minute' WHERE id = :i"),
        {"i": inv_pk},
    )
    assert (await api.accept(invitee, inv["token"])).status_code == 400
    listed = (await api.c.get(f"/v1/orgs/{oid}/invitations", headers=owner.h)).json()
    assert [i["status"] for i in listed if i["id"] == inv["id"]] == ["expired"]


async def test_revoked_invitation_rejected(api: Api, org: tuple[Actor, str]) -> None:
    owner, oid = org
    inv = await invite_token(api, oid, owner, "gone@example.com")
    invitee = await api.actor("gone@example.com")
    r = await api.c.delete(f"/v1/orgs/{oid}/invitations/{inv['id']}", headers=owner.h)
    assert r.status_code == 204
    assert (await api.accept(invitee, inv["token"])).status_code == 400
    listed = (await api.c.get(f"/v1/orgs/{oid}/invitations", headers=owner.h)).json()
    assert [i["status"] for i in listed] == ["revoked"]


async def test_accepted_invitation_cannot_be_revoked(api: Api, org: tuple[Actor, str]) -> None:
    owner, oid = org
    inv = await invite_token(api, oid, owner, "done@example.com")
    await api.accept(await api.actor("done@example.com"), inv["token"])
    r = await api.c.delete(f"/v1/orgs/{oid}/invitations/{inv['id']}", headers=owner.h)
    assert r.status_code == 409


async def test_inviter_demoted_before_accept_blocks_the_invitation(
    api: Api, org: tuple[Actor, str]
) -> None:
    owner, oid = org
    inviter = await api.add_member(oid, owner, "inviter@example.com", "admin")
    inv = await invite_token(api, oid, inviter, "new@example.com", "admin")
    invitee = await api.actor("new@example.com")
    member = await api.member_id(oid, owner, "inviter@example.com")
    r = await api.c.patch(
        f"/v1/orgs/{oid}/members/{member}", json={"role": "viewer"}, headers=owner.h
    )
    assert r.status_code == 200
    r = await api.accept(invitee, inv["token"])
    assert r.status_code == 403
    mine = (await api.c.get("/v1/me/organizations", headers=invitee.h)).json()
    assert mine == []


async def test_inviter_removed_before_accept_blocks_the_invitation(
    api: Api, org: tuple[Actor, str]
) -> None:
    owner, oid = org
    inviter = await api.add_member(oid, owner, "inviter@example.com", "admin")
    inv = await invite_token(api, oid, inviter, "new@example.com", "viewer")
    invitee = await api.actor("new@example.com")
    member = await api.member_id(oid, owner, "inviter@example.com")
    assert (
        await api.c.delete(f"/v1/orgs/{oid}/members/{member}", headers=owner.h)
    ).status_code == 204
    assert (await api.accept(invitee, inv["token"])).status_code in {400, 403}
    assert (await api.c.get("/v1/me/organizations", headers=invitee.h)).json() == []


async def test_already_member_cannot_accept(api: Api, org: tuple[Actor, str]) -> None:
    owner, oid = org
    member = await api.add_member(oid, owner, "m@example.com", "viewer")
    inv = await invite_token(api, oid, owner, "m@example.com", "admin")
    r = await api.accept(member, inv["token"])
    assert r.status_code == 409
    mine = (await api.c.get("/v1/me/organizations", headers=member.h)).json()
    assert [o["role"] for o in mine] == ["viewer"]  # no silent role change


async def test_token_visible_in_test_env_and_never_in_lists(
    api: Api, org: tuple[Actor, str]
) -> None:
    owner, oid = org
    inv = await invite_token(api, oid, owner, "v@example.com")
    assert isinstance(inv["token"], str) and len(inv["token"]) >= 32
    listed = await api.c.get(f"/v1/orgs/{oid}/invitations", headers=owner.h)
    assert inv["token"] not in listed.text
    assert all("token" not in i for i in listed.json())


async def test_token_absent_when_exposure_disabled(
    api: Api, org: tuple[Actor, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, oid = org
    monkeypatch.setattr(Settings, "expose_invite_tokens", property(lambda _self: False))
    r = await api.invite(oid, owner, "hidden@example.com", "viewer")
    assert r.status_code == 201
    assert "token" not in r.json()
    assert r.json()["status"] == "pending"


async def test_only_a_hash_is_stored(
    api: Api, org: tuple[Actor, str], db_session: AsyncSession
) -> None:
    owner, oid = org
    inv = await invite_token(api, oid, owner, "h@example.com")
    token = inv["token"]
    await api.accept(await api.actor("h@example.com"), token)
    stored = (await db_session.execute(text("SELECT token_hash FROM invitations"))).scalars().all()
    assert stored == [hashlib.sha256(token.encode()).hexdigest()]
    for table in ("invitations", "audit_log"):
        n = (
            await db_session.execute(
                text(f"SELECT count(*) FROM {table} t WHERE t::text LIKE :p"), {"p": f"%{token}%"}
            )
        ).scalar_one()
        assert n == 0, table
    audit = await api.c.get(f"/v1/orgs/{oid}/audit-log", headers=owner.h)
    assert token not in audit.text


async def test_invitation_email_is_normalised_and_inviter_is_the_caller(
    api: Api, org: tuple[Actor, str]
) -> None:
    owner, oid = org
    invitee = await api.actor("mixed@example.com")
    inv = await invite_token(api, oid, owner, "Mixed@Example.com")
    assert inv["email"] == "mixed@example.com"
    assert (await api.accept(invitee, inv["token"])).status_code == 200
