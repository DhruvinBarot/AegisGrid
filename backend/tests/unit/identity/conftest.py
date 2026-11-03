from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.organizations.support import World

CSRF = {"X-AegisGrid-CSRF": "1"}
PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
async def clean_rate_limits(redis: Redis) -> AsyncIterator[None]:
    async def wipe() -> None:
        async for key in redis.scan_iter("rl:*"):
            await redis.delete(key)

    await wipe()
    yield
    await wipe()


async def register(client: AsyncClient, email: str = "ada@example.com") -> Response:
    return await client.post(
        "/v1/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": "Ada"},
    )


async def login(
    client: AsyncClient, email: str = "ada@example.com", password: str = PASSWORD
) -> Response:
    return await client.post(
        "/v1/auth/login", json={"email": email, "password": password}, headers=CSRF
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def session_tokens(client: AsyncClient) -> dict[str, str]:
    """Register + login; returns the access token and the refresh cookie value."""
    assert (await register(client)).status_code == 201
    r = await login(client)
    assert r.status_code == 200
    refresh = r.cookies["ag_refresh"]
    client.cookies.clear()  # tests send the cookie explicitly
    return {"access": r.json()["access_token"], "refresh": refresh}


@pytest.fixture
def world(app: FastAPI, client: AsyncClient, db_session: AsyncSession) -> World:
    return World(app, client, db_session)
