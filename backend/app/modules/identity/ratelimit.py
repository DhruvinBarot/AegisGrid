"""Fixed-window rate limiting on Redis for the unauthenticated auth endpoints.

Redis down: staging/production fail CLOSED (503); development/test fail OPEN with a loud warning.
"""

import hashlib
import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.cache import redis
from app.core.config import settings
from app.core.errors import ProblemError

log = logging.getLogger(__name__)


def get_redis() -> Redis:
    return redis


def email_key(email: str) -> str:
    """Hashed so no address sits in Redis."""
    return "email:" + hashlib.sha256(email.encode()).hexdigest()[:32]


async def enforce(client: Redis, route: str, *buckets: tuple[str, int]) -> None:
    """Count one attempt in each (key, limit) bucket; 429 with Retry-After when one is over."""
    window = settings.rate_limit_window_seconds
    try:
        for key, limit in buckets:
            full = f"rl:{route}:{key}"
            pipe = client.pipeline()
            pipe.incr(full)
            pipe.ttl(full)
            count, ttl = await pipe.execute()
            if ttl < 0:  # first hit, or an expire that was lost: never leave a key without TTL
                await client.expire(full, window)
                ttl = window
            if count > limit:
                raise ProblemError(
                    "Too many attempts. Try again later.",
                    status=429,
                    title="Too Many Requests",
                    headers={"Retry-After": str(ttl)},
                )
    except (RedisError, OSError) as exc:
        if settings.env in {"staging", "production"}:
            raise ProblemError(
                "Service temporarily unavailable.", status=503, title="Service Unavailable"
            ) from exc
        log.warning(
            "rate limiting unavailable, failing open",
            extra={"event": "rate_limit_unavailable", "env": settings.env, "route": route},
        )
