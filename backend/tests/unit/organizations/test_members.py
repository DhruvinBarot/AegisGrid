from datetime import UTC, datetime, timedelta

from httpx import Response
from sqlalchemy import select, update

from app.core.tenancy import system_scope
from app.modules.organizations.models import Invitation
from tests.unit.organizations.support import Actor, World, uid


async def mem_id(w: World, viewer: Actor, org: str, target: Actor) -> str:
    rows = (await w.req(viewer, "GET", f"/orgs/{org}/members")).json()
    return next(m["id"] for m in rows if m["email"] == target.email)


async def setup(w: World) -> tuple[str, Actor, Actor, Actor]:
    owner = await w.user()
    org = await w.make_org(owner)
    return org, owner, await w.member(org, "admin"), await w.member(org, "developer")


async def test_grant_rules(world: World) -> None:
    org, owner, admin, dev = await setup(world)
    dev_id = await mem_id(world, owner, org, dev)
    owner_id = await mem_id(world, owner, org, owner)
    admin_id = await mem_id(world, owner, org, admin)

    async def patch(a: Actor, m: str, role: str) -> Response:
        return await world.req(a, "PATCH", f"/orgs/{org}/members/{m}", json={"role": role})

    assert (await patch(admin, dev_id, "owner")).status_code == 403  # only an owner grants owner
    assert (await patch(admin, dev_id, "admin")).json()["role"] == "admin"  # at own level is fine
    assert (await patch(dev, dev_id, "viewer")).status_code == 403  # no members:write
    assert (await patch(admin, owner_id, "viewer")).status_code == 403  # cannot touch higher rank
    assert (await patch(admin, admin_id, "viewer")).status_code == 403  # nobody changes own role
    assert (await patch(owner, owner_id, "admin")).status_code == 403  # not even an owner
    assert (await patch(owner, dev_id, "owner")).json()["role"] == "owner"


async def test_last_owner_protection(world: World) -> None:
    org, owner, admin, _ = await setup(world)
    owner_id = await mem_id(world, owner, org, owner)
    # Sole owner cannot leave or be demoted.
    r = await world.req(owner, "DELETE", f"/orgs/{org}/members/{owner_id}")
    assert r.status_code == 409 and r.json()["code"] == "last_owner"

    second = await world.member(org, "owner")
    second_id = await mem_id(world, owner, org, second)
    # Two owners: demoting one is fine (done by the other) ...
    r = await world.req(second, "PATCH", f"/orgs/{org}/members/{owner_id}", json={"role": "admin"})
    assert r.status_code == 200
    # ... and the remaining owner is now the last one.
    r = await world.req(second, "DELETE", f"/orgs/{org}/members/{second_id}")
    assert r.status_code == 409
    # An admin cannot remove an owner regardless of count.
    assert (await world.req(admin, "DELETE", f"/orgs/{org}/members/{second_id}")).status_code == 403


async def test_leave_org_and_remove_revokes_invites(world: World) -> None:
    org, owner, admin, dev = await setup(world)
    admin_id = await mem_id(world, owner, org, admin)
    inv = (
        await world.req(
            admin, "POST", f"/orgs/{org}/invitations", json={"email": "a@b.cc", "role": "viewer"}
        )
    ).json()
    assert (await world.req(dev, "DELETE", f"/orgs/{org}/members/{admin_id}")).status_code == 403
    assert (await world.req(owner, "DELETE", f"/orgs/{org}/members/{admin_id}")).status_code == 204
    rows = (await world.req(owner, "GET", f"/orgs/{org}/invitations")).json()
    assert [r["status"] for r in rows if r["id"] == inv["id"]] == ["revoked"]
    assert (await world.req(admin, "GET", f"/orgs/{org}")).status_code == 404  # no longer a member

    dev_id = await mem_id(world, owner, org, dev)
    assert (await world.req(dev, "DELETE", f"/orgs/{org}/members/{dev_id}")).status_code == 204


async def test_service_principal_rejected_by_member_endpoints(world: World) -> None:
    org, owner, *_ = await setup(world)
    svc = await world.service(uid(org), {"org:read"})
    with system_scope():
        from app.modules.organizations.models import Membership

        row = await world.session.scalar(
            select(Membership).where(Membership.principal_id == svc.principal.principal_id)
        )
    assert row is not None
    from app.core.ids import encode_id

    sid = encode_id("mem", row.id)
    listed = (await world.req(owner, "GET", f"/orgs/{org}/members")).json()
    assert sid not in {m["id"] for m in listed}
    for method, body in (("PATCH", {"role": "admin"}), ("DELETE", None)):
        r = await world.req(owner, method, f"/orgs/{org}/members/{sid}", json=body)
        assert r.status_code == 422 and r.json()["code"] == "service_principal"
    # Service principals cannot accept invitations either.
    assert (
        await world.req(svc, "POST", "/invitations/accept", json={"token": "x"})
    ).status_code == 403


async def test_api_key_cannot_manage_members(world: World) -> None:
    org, owner, _, dev = await setup(world)
    svc = await world.service(uid(org), {"members:write", "members:read"}, "admin")
    r = await world.req(
        svc, "POST", f"/orgs/{org}/invitations", json={"email": "a@b.cc", "role": "viewer"}
    )
    assert r.status_code == 403
    dev_id = await mem_id(world, owner, org, dev)
    r = await world.req(svc, "PATCH", f"/orgs/{org}/members/{dev_id}", json={"role": "viewer"})
    assert r.status_code == 403


async def invite(w: World, inviter: Actor, org: str, email: str, role: str = "developer") -> dict:  # noqa: ANN401
    r = await w.req(
        inviter, "POST", f"/orgs/{org}/invitations", json={"email": email, "role": role}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_invitation_grant_rules(world: World) -> None:
    org, owner, admin, dev = await setup(world)
    body = {"email": "a@b.cc", "role": "owner"}
    assert (
        await world.req(admin, "POST", f"/orgs/{org}/invitations", json=body)
    ).status_code == 403
    assert (
        await world.req(owner, "POST", f"/orgs/{org}/invitations", json=body)
    ).status_code == 201
    body["role"] = "viewer"
    assert (await world.req(dev, "POST", f"/orgs/{org}/invitations", json=body)).status_code == 403
    bad = {"email": "nope", "role": "viewer"}
    assert (await world.req(owner, "POST", f"/orgs/{org}/invitations", json=bad)).status_code == 422


async def test_invitation_lifecycle(world: World) -> None:
    org, owner, admin, _ = await setup(world)
    guest = await world.user("Guest@Example.com")
    token = (await invite(world, admin, org, "guest@example.com"))["token"]

    accept = lambda who, tok: world.req(who, "POST", "/invitations/accept", json={"token": tok})  # noqa: E731
    stranger = await world.user()
    assert (await accept(stranger, token)).status_code == 400  # email mismatch
    assert (await accept(guest, "nonsense")).status_code == 400
    r = await accept(guest, token)
    assert r.status_code == 200 and r.json() == {"organization_id": org, "role": "developer"}
    assert (await accept(guest, token)).status_code == 400  # single use
    assert (await world.req(guest, "GET", f"/orgs/{org}")).status_code == 200


async def test_invitation_expired_revoked_and_inviter_demoted(world: World) -> None:
    org, owner, admin, _ = await setup(world)
    guest = await world.user("g@example.com")
    accept = lambda tok: world.req(guest, "POST", "/invitations/accept", json={"token": tok})  # noqa: E731

    expired = await invite(world, owner, org, "g@example.com")
    with system_scope():
        await world.session.execute(
            update(Invitation).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert (await accept(expired["token"])).status_code == 400

    revoked = await invite(world, owner, org, "g@example.com")
    r = await world.req(owner, "DELETE", f"/orgs/{org}/invitations/{revoked['id']}")
    assert r.status_code == 204
    assert (await accept(revoked["token"])).status_code == 400

    # Inviter (admin) invites an admin, then is demoted below the invited role before accept.
    pending = await invite(world, admin, org, "g@example.com", "admin")
    admin_id = await mem_id(world, owner, org, admin)
    await world.req(owner, "PATCH", f"/orgs/{org}/members/{admin_id}", json={"role": "developer"})
    r = await accept(pending["token"])
    assert r.status_code == 403 and r.json()["code"] == "inviter_not_authorized"
    with system_scope():
        stored = await world.session.scalar(select(Invitation).where(Invitation.role == "admin"))
    assert stored is not None and stored.accepted_at is None


async def test_invitation_stores_only_hash(world: World) -> None:
    org, owner, *_ = await setup(world)
    token = (await invite(world, owner, org, "h@example.com"))["token"]
    with system_scope():
        rows = (await world.session.scalars(select(Invitation))).all()
    assert all(token not in (r.token_hash, r.email) for r in rows)
    assert all(len(r.token_hash) == 64 for r in rows)
    first = next(r for r in rows if r.email == "h@example.com")
    assert first.invited_by == owner.principal.principal_id
