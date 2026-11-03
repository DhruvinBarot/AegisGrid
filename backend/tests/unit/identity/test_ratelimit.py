import logging

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from redis.asyncio import Redis

from app.core.config import settings
from app.modules.identity import ratelimit
from tests.unit.identity.conftest import login, register


@pytest.fixture
def broken_redis(app: FastAPI) -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    app.dependency_overrides[ratelimit.get_redis] = lambda: dead


async def test_login_rate_limited_per_email(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "login_email_limit", 2)
    await register(client)
    assert (await login(client, password="x")).status_code == 401
    assert (await login(client, password="x")).status_code == 401
    r = await login(client)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    # Another address is unaffected.
    assert (await login(client, "other@example.com", "x")).status_code == 401


async def test_login_rate_limited_per_ip(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "login_ip_limit", 2)
    for email in ("a@example.com", "b@example.com"):
        assert (await login(client, email, "x")).status_code == 401
    assert (await login(client, "c@example.com", "x")).status_code == 429


async def test_register_rate_limited_per_ip(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "register_ip_limit", 1)
    assert (await register(client, "a@example.com")).status_code == 201
    assert (await register(client, "b@example.com")).status_code == 429


@pytest.mark.usefixtures("broken_redis")
async def test_redis_down_fails_open_with_warning_in_test_env(
    client: AsyncClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # alembic's fileConfig (run by the migration fixture) disables pre-existing loggers.
    monkeypatch.setattr(ratelimit.log, "disabled", False)
    assert (await register(client)).status_code == 201
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.modules.identity.ratelimit"):
        r = await login(client)
    assert r.status_code == 200
    rec = next(x for x in caplog.records if getattr(x, "event", "") == "rate_limit_unavailable")
    assert rec.levelno == logging.WARNING
    assert (rec.__dict__["env"], rec.__dict__["route"]) == ("test", "login")


@pytest.mark.usefixtures("broken_redis")
@pytest.mark.parametrize("env", ["production", "staging"])
async def test_redis_down_fails_closed_in_prod(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    monkeypatch.setattr(settings, "env", env)
    r = await login(client)
    assert r.status_code == 503
    assert r.headers["content-type"].startswith("application/problem+json")
