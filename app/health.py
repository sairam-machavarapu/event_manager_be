import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from app.config import get_settings
from app.database import engine

router = APIRouter(prefix="/api/v1/health", tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok", "service": "gather-api"}


async def check_database() -> bool:
    try:
        async with asyncio.timeout(4), engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.warning("Database readiness probe failed")
        return False


async def check_redis(url: str) -> bool:
    client = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
    try:
        return bool(await client.ping())
    except Exception:
        logger.warning("Redis readiness probe failed")
        return False
    finally:
        await client.aclose()


@router.get("/ready")
async def readiness() -> JSONResponse:
    settings = get_settings()
    checks = await asyncio.gather(
        check_database(), check_redis(settings.redis_url), check_redis(settings.celery_broker_url)
    )
    ready = all(checks)
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "unavailable",
            "checks": dict(zip(["database", "cache", "broker"], checks, strict=True)),
        },
    )
