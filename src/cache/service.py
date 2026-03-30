import orjson
from src.core.logging import get_logger

logger = get_logger(component="cache")


class CacheService:
    def __init__(self, redis_client, default_ttl: int = 30):
        self._redis = redis_client
        self._default_ttl = default_ttl

    async def get(self, key: str) -> dict | None:
        try:
            data = await self._redis.get(key)
            if data is None:
                return None
            return orjson.loads(data)
        except Exception as e:
            logger.warning("cache_get_error", key=key, error=str(e))
            return None

    async def set(self, key: str, value: dict, ttl: int | None = None) -> None:
        try:
            await self._redis.set(key, orjson.dumps(value), ex=ttl if ttl is not None else self._default_ttl)
        except Exception as e:
            logger.warning("cache_set_error", key=key, error=str(e))
