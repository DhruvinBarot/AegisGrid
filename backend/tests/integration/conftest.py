from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis

from app.core.config import settings
from tests.integration.helpers import Api


@pytest.fixture(autouse=True)
async def _isolated_rate_limits(
    redis: Redis, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """Every test registers/logs in from one fake IP: lift the limits, except in the tests that
    exercise them (they lower the limits again)."""
    for name in ("login_ip_limit", "login_email_limit", "register_ip_limit"):
        monkeypatch.setattr(settings, name, 10_000)

    async def wipe() -> None:
        async for key in redis.scan_iter("rl:*"):
            await redis.delete(key)

    await wipe()
    yield
    await wipe()


@pytest.fixture
def api(client: AsyncClient) -> Api:
    return Api(client)
