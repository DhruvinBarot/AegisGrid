from redis.asyncio import Redis

from app.core.config import settings

# Ephemeral only (rate limits, pub/sub, TTL cache). Never durable state:
# see docs/adr/0002-postgres-source-of-truth.md.
redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
