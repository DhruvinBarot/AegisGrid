"""Login rate limiting and its Redis-outage policy."""

import logging

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from redis.asyncio import Redis

from app.core.config import settings
from app.modules.identity import ratelimit
from tests.integration.helpers import CSRF, PASSWORD, Api


async def bad_login(client: AsyncClient, email: str = "x@example.com"):
    return await client.post(
        "/v1/auth/login", json={"email": email, "password": "wrong-password"}, headers=CSRF
    )


@pytest.fixture
def dead_redis(app: FastAPI) -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    app.dependency_overrides[ratelimit.get_redis] = lambda: dead


async def test_exceeding_login_limit_gives_429_with_retry_after(
    client: AsyncClient, api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "login_email_limit", 3)
    await api.actor("rl@example.com")  # one successful login counts as the first attempt
    for _ in range(2):
        assert (await bad_login(client, "rl@example.com")).status_code == 401
    r = await bad_login(client, "rl@example.com")
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    assert r.headers["content-type"].startswith("application/problem+json")
    # blocked even with the right password: the limiter runs before the check
    ok = await client.post(
        "/v1/auth/login", json={"email": "rl@example.com", "password": PASSWORD}, headers=CSRF
    )
    assert ok.status_code == 429
    # another address is not affected
    assert (await bad_login(client, "someone-else@example.com")).status_code == 401


async def test_ip_limit_applies_across_addresses(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "login_ip_limit", 3)
    codes = [(await bad_login(client, f"u{i}@example.com")).status_code for i in range(4)]
    assert codes == [401, 401, 401, 429]


async def test_email_bucket_is_case_insensitive(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "login_email_limit", 2)
    codes = [
        (await bad_login(client, e)).status_code
        for e in ("Case@Example.com", "case@example.com", "CASE@EXAMPLE.COM")
    ]
    assert codes == [401, 401, 429]


async def test_register_is_rate_limited(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "register_ip_limit", 2)
    codes = []
    for i in range(3):
        r = await client.post(
            "/v1/auth/register",
            json={"email": f"r{i}@example.com", "password": PASSWORD, "display_name": "R"},
        )
        codes.append(r.status_code)
    assert codes == [201, 201, 429]


@pytest.mark.usefixtures("dead_redis")
@pytest.mark.parametrize("env", ["production", "staging"])
async def test_redis_down_fails_closed_outside_dev(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    monkeypatch.setattr(settings, "env", env)
    r = await bad_login(client)
    assert r.status_code == 503
    assert r.headers["content-type"].startswith("application/problem+json")


@pytest.mark.usefixtures("dead_redis")
async def test_redis_down_fails_open_with_warning_in_test_env(
    client: AsyncClient,
    api: Api,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert settings.env == "test"
    # alembic's fileConfig (run by the session migration fixture) disables pre-existing loggers
    monkeypatch.setattr(logging.getLogger(ratelimit.__name__), "disabled", False)
    r = await client.post(
        "/v1/auth/register",
        json={"email": "open@example.com", "password": PASSWORD, "display_name": "O"},
    )
    assert r.status_code == 201  # not blocked by the outage
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        r = await bad_login(client, "open@example.com")
    assert r.status_code == 401  # reached the credential check, not 503
    records = [x for x in caplog.records if getattr(x, "event", "") == "rate_limit_unavailable"]
    assert records, "no structured warning for the failed-open limiter"
    assert records[0].levelno == logging.WARNING
    assert getattr(records[0], "route", None) == "login"
