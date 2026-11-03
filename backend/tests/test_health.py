from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import db


async def test_healthz(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readyz_with_services_up(client: AsyncClient) -> None:
    # Requires the compose postgres + redis (see README).
    resp = await client.get("/readyz")
    assert resp.status_code == 200, resp.json()
    assert resp.json()["checks"] == {"postgres": "ok", "redis": "ok"}


async def test_readyz_503_when_postgres_down(client: AsyncClient, monkeypatch) -> None:
    dead = create_async_engine(
        "postgresql+psycopg://x:x@127.0.0.1:1/x", connect_args={"connect_timeout": 1}
    )
    monkeypatch.setattr(db, "engine", dead)
    resp = await client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["postgres"] == "unavailable"
    await dead.dispose()
