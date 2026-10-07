"""Optional Redis acceleration; database generations make invalidation durable."""

import hashlib

from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from app.config import get_settings

TTL = 30


def key(kind, version, filters=""):
    digest = hashlib.sha256(filters.encode()).hexdigest()
    return f"gather:discovery:v1:{version}:{kind}:{digest}"


async def read(cache_key):
    client = Redis.from_url(
        get_settings().redis_url,
        socket_connect_timeout=0.2,
        socket_timeout=0.2,
        retry=Retry(NoBackoff(), 0),
    )
    try:
        return await client.get(cache_key)
    except RedisError, OSError:
        return None
    finally:
        await client.aclose()


async def write(cache_key, value):
    client = Redis.from_url(
        get_settings().redis_url,
        socket_connect_timeout=0.2,
        socket_timeout=0.2,
        retry=Retry(NoBackoff(), 0),
    )
    try:
        await client.setex(cache_key, TTL, value)
    except RedisError, OSError:
        pass
    finally:
        await client.aclose()
