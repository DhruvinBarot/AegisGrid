from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditLog
from app.modules.identity.models import AuthSession, RefreshToken
from tests.unit.identity.conftest import CSRF, PASSWORD, bearer, login, register


def cookie(token: str) -> dict[str, str]:
    return {**CSRF, "Cookie": f"ag_refresh={token}"}


async def actions(db: AsyncSession) -> list[tuple[str, str]]:
    rows = await db.execute(select(AuditLog.action, AuditLog.outcome))
    return [(a, o) for a, o in rows.all()]


async def test_register_login_me(client: AsyncClient, db_session: AsyncSession) -> None:
    r = await register(client, "Ada@Example.COM")
    assert r.status_code == 201
    assert r.json()["email"] == "ada@example.com"
    assert (await register(client, "ada@example.com")).status_code == 409

    r = await login(client, "ADA@example.com")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=strict" in set_cookie
    assert "path=/v1/auth/refresh" in set_cookie

    me = await client.get("/v1/me", headers=bearer(r.json()["access_token"]))
    assert me.status_code == 200
    assert me.json()["email"] == "ada@example.com"
    assert ("auth.register", "success") in await actions(db_session)


async def test_wrong_password_and_unknown_email_are_identical(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await register(client)
    bad_pw = await login(client, password="not the password")
    unknown = await login(client, "nobody@example.com")
    assert bad_pw.status_code == unknown.status_code == 401
    assert bad_pw.json() == unknown.json()
    assert (await actions(db_session)).count(("auth.login", "failure")) == 2


async def test_login_requires_csrf_header_and_origin(client: AsyncClient) -> None:
    await register(client)
    body = {"email": "ada@example.com", "password": PASSWORD}
    assert (await client.post("/v1/auth/login", json=body)).status_code == 403
    bad_origin = {**CSRF, "Origin": "https://evil.example"}
    assert (await client.post("/v1/auth/login", json=body, headers=bad_origin)).status_code == 403
    ok_origin = {**CSRF, "Origin": "http://localhost:3000"}
    assert (await client.post("/v1/auth/login", json=body, headers=ok_origin)).status_code == 200


async def test_me_without_credentials(client: AsyncClient) -> None:
    r = await client.get("/v1/me")
    assert r.status_code == 401
    assert (await client.get("/v1/me", headers=bearer("garbage"))).json() == r.json()


async def test_refresh_rotates(client: AsyncClient, session_tokens: dict[str, str]) -> None:
    r = await client.post("/v1/auth/refresh", headers=cookie(session_tokens["refresh"]))
    assert r.status_code == 200
    assert r.cookies["ag_refresh"] != session_tokens["refresh"]
    me = await client.get("/v1/me", headers=bearer(r.json()["access_token"]))
    assert me.status_code == 200


async def test_refresh_requires_csrf(client: AsyncClient, session_tokens: dict[str, str]) -> None:
    cookie_only = {"Cookie": f"ag_refresh={session_tokens['refresh']}"}
    assert (await client.post("/v1/auth/refresh", headers=cookie_only)).status_code == 403


async def test_refresh_reuse_revokes_family_and_session(
    client: AsyncClient, session_tokens: dict[str, str], db_session: AsyncSession
) -> None:
    first = session_tokens["refresh"]
    second = (await client.post("/v1/auth/refresh", headers=cookie(first))).cookies["ag_refresh"]
    assert (await client.post("/v1/auth/refresh", headers=cookie(first))).status_code == 401

    # The legitimate newest token and the access token are dead too.
    assert (await client.post("/v1/auth/refresh", headers=cookie(second))).status_code == 401
    assert (await client.get("/v1/me", headers=bearer(session_tokens["access"]))).status_code == 401
    assert ("auth.refresh_reuse_detected", "failure") in await actions(db_session)
    live = await db_session.scalars(select(RefreshToken).where(RefreshToken.revoked_at.is_(None)))
    assert list(live) == []


async def test_refresh_expiry(
    client: AsyncClient, session_tokens: dict[str, str], db_session: AsyncSession
) -> None:
    await db_session.execute(
        update(RefreshToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    r = await client.post("/v1/auth/refresh", headers=cookie(session_tokens["refresh"]))
    assert r.status_code == 401


async def test_refresh_without_cookie(client: AsyncClient) -> None:
    assert (await client.post("/v1/auth/refresh", headers=CSRF)).status_code == 401


async def test_logout_revokes_session(client: AsyncClient, session_tokens: dict[str, str]) -> None:
    h = {**CSRF, **bearer(session_tokens["access"])}
    assert (await client.post("/v1/auth/logout", headers=h)).status_code == 204
    assert (await client.get("/v1/me", headers=bearer(session_tokens["access"]))).status_code == 401
    r = await client.post("/v1/auth/refresh", headers=cookie(session_tokens["refresh"]))
    assert r.status_code == 401


async def test_logout_all_revokes_every_session(
    client: AsyncClient, session_tokens: dict[str, str], db_session: AsyncSession
) -> None:
    other = (await login(client)).json()["access_token"]
    h = {**CSRF, **bearer(session_tokens["access"])}
    assert (await client.post("/v1/auth/logout-all", headers=h)).status_code == 204
    for token in (session_tokens["access"], other):
        assert (await client.get("/v1/me", headers=bearer(token))).status_code == 401
    assert ("auth.logout_all", "success") in await actions(db_session)


async def test_absolute_session_expiry(
    client: AsyncClient, session_tokens: dict[str, str], db_session: AsyncSession
) -> None:
    await db_session.execute(
        update(AuthSession).values(absolute_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    assert (await client.get("/v1/me", headers=bearer(session_tokens["access"]))).status_code == 401
    r = await client.post("/v1/auth/refresh", headers=cookie(session_tokens["refresh"]))
    assert r.status_code == 401
