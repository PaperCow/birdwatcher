import pytest
import time
from unittest.mock import AsyncMock
from fakeredis import FakeAsyncRedis
from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from src.middleware.rate_limit import RateLimitMiddleware


async def homepage(request):
    return PlainTextResponse("ok")


async def health(request):
    return PlainTextResponse("healthy")


def _make_app(redis_client, max_requests=5, window=60):
    app = Starlette(routes=[
        Route("/", homepage),
        Route("/health", health),
    ])
    return RateLimitMiddleware(app, max_requests=max_requests, window=window, redis_client=redis_client)


@pytest.fixture
async def redis():
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.mark.unit
class TestRateLimiter:
    async def test_requests_within_limit_pass(self, redis):
        from httpx import AsyncClient, ASGITransport
        app = _make_app(redis, max_requests=5)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(5):
                resp = await client.get("/")
                assert resp.status_code == 200

    async def test_exceeding_limit_returns_429(self, redis):
        from httpx import AsyncClient, ASGITransport
        app = _make_app(redis, max_requests=3)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(3):
                await client.get("/")
            resp = await client.get("/")
            assert resp.status_code == 429
            assert "Retry-After" in resp.headers

    async def test_health_excluded(self, redis):
        from httpx import AsyncClient, ASGITransport
        app = _make_app(redis, max_requests=1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.get("/")  # uses up the limit
            resp = await client.get("/health")
            assert resp.status_code == 200  # health excluded

    async def test_redis_error_fails_open(self):
        from httpx import AsyncClient, ASGITransport
        broken_redis = AsyncMock()
        broken_redis.pipeline = lambda: _broken_pipeline()
        app = _make_app(broken_redis, max_requests=1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/")
            assert resp.status_code == 200  # fail open


def _broken_pipeline():
    pipe = AsyncMock()
    pipe.incr = AsyncMock()
    pipe.expire = AsyncMock()
    pipe.execute = AsyncMock(side_effect=ConnectionError("down"))
    return pipe
