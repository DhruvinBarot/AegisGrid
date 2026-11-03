"""Refresh-token rotation and reuse detection (ADR-0005)."""

from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.helpers import CSRF, Actor, Api


async def refresh(client: AsyncClient, token: str, **headers: str) -> Response:
    return await client.post(
        "/v1/auth/refresh", headers={**CSRF, "Cookie": f"ag_refresh={token}", **headers}
    )


async def me(client: AsyncClient, access: str) -> int:
    return (await client.get("/v1/me", headers={"Authorization": f"Bearer {access}"})).status_code


async def test_rotation_issues_new_pair_and_cookie_attributes(
    client: AsyncClient, api: Api
) -> None:
    a = await api.actor("rot@example.com")
    r = await refresh(client, a.refresh)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    new_access = r.json()["access_token"]
    new_refresh = r.cookies["ag_refresh"]
    assert new_refresh != a.refresh
    assert await me(client, new_access) == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert "path=/v1/auth/refresh" in cookie
    # chain continues
    assert (await refresh(client, new_refresh)).status_code == 200


async def test_reuse_revokes_family_session_and_audits(
    client: AsyncClient, api: Api, db_session: AsyncSession
) -> None:
    a = await api.actor("reuse@example.com")
    first = await refresh(client, a.refresh)
    assert first.status_code == 200
    legit_refresh = first.cookies["ag_refresh"]
    legit_access = first.json()["access_token"]
    assert await me(client, legit_access) == 200

    replay = await refresh(client, a.refresh)  # the already-used token
    assert replay.status_code == 401

    # the whole family and the session are dead, including what the legitimate holder got
    assert (await refresh(client, legit_refresh)).status_code == 401
    assert await me(client, a.access) == 401
    assert await me(client, legit_access) == 401

    rows = (
        await db_session.execute(
            text(
                "SELECT outcome, target_type FROM audit_log WHERE action = :a "
                "AND actor_id = (SELECT p.id FROM principals p JOIN users u ON u.id = p.user_id "
                "WHERE u.email = 'reuse@example.com')"
            ),
            {"a": "auth.refresh_reuse_detected"},
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].outcome == "failure" and rows[0].target_type == "session"


async def test_reuse_leaves_other_sessions_alone(client: AsyncClient, api: Api) -> None:
    a = await api.actor("two@example.com")
    b = await api.login(a.email)
    assert (await refresh(client, a.refresh)).status_code == 200
    assert (await refresh(client, a.refresh)).status_code == 401
    assert await me(client, b.access) == 200


async def test_refresh_requires_csrf_header(client: AsyncClient, api: Api) -> None:
    a = await api.actor("nocsrf@example.com")
    r = await client.post("/v1/auth/refresh", headers={"Cookie": f"ag_refresh={a.refresh}"})
    assert r.status_code == 403
    # the rejected call did not consume the token
    assert (await refresh(client, a.refresh)).status_code == 200


async def test_refresh_rejects_foreign_origin(client: AsyncClient, api: Api) -> None:
    a = await api.actor("origin@example.com")
    r = await refresh(client, a.refresh, Origin="https://evil.example")
    assert r.status_code == 403
    assert (await refresh(client, a.refresh, Origin="http://localhost:3000")).status_code == 200


async def test_refresh_rejects_missing_unknown_and_access_token(
    client: AsyncClient, api: Api
) -> None:
    a: Actor = await api.actor("bad@example.com")
    assert (await client.post("/v1/auth/refresh", headers=CSRF)).status_code == 401
    assert (await refresh(client, "not-a-real-token")).status_code == 401
    assert (
        await refresh(client, a.access)
    ).status_code == 401  # an access JWT is not a refresh token


async def test_refresh_token_not_accepted_as_bearer(client: AsyncClient, api: Api) -> None:
    a = await api.actor("swap@example.com")
    assert await me(client, a.refresh) == 401
