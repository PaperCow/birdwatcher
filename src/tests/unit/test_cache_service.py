import pytest
from unittest.mock import AsyncMock
from fakeredis import FakeAsyncRedis
from src.core.cache import CacheService


@pytest.fixture
async def redis():
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.mark.unit
class TestCacheService:
    async def test_get_miss_returns_none(self, redis):
        cache = CacheService(redis, default_ttl=30)
        assert await cache.get("nonexistent") is None

    async def test_set_and_get_roundtrip(self, redis):
        cache = CacheService(redis, default_ttl=30)
        data = {"buckets": [{"count": 5}]}
        await cache.set("key1", data)
        result = await cache.get("key1")
        assert result == data

    async def test_get_redis_error_returns_none(self):
        broken_redis = AsyncMock()
        broken_redis.get = AsyncMock(side_effect=ConnectionError("down"))
        cache = CacheService(broken_redis, default_ttl=30)
        assert await cache.get("key") is None

    async def test_set_redis_error_swallowed(self):
        broken_redis = AsyncMock()
        broken_redis.set = AsyncMock(side_effect=ConnectionError("down"))
        cache = CacheService(broken_redis, default_ttl=30)
        await cache.set("key", {"data": 1})  # should not raise
