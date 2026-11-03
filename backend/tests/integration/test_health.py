"""Liveness and readiness probes."""

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis


async def test_healthz_is_unauthenticated_and_cheap(client: AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_readyz_reports_dependencies(client: AsyncClient) -> None:
    r = await client.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "checks": {"postgres": "ok", "redis": "ok"}}


async def test_readyz_stays_ready_when_only_redis_is_down(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The API keeps serving without Redis (rate limiting degrades), so it must stay in rotation.
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    monkeypatch.setattr("app.main.redis", dead)
    r = await client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["checks"] == {"postgres": "ok", "redis": "unavailable"}
    assert r.json()["status"] == "ready"


async def test_readyz_is_not_ready_when_postgres_is_down(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken:
        def connect(self) -> None:
            raise OSError("db down")

    monkeypatch.setattr("app.main.db.engine", Broken())
    r = await client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["status"] == "not_ready"
    assert r.json()["checks"]["postgres"] == "unavailable"
