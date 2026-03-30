import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.routing import Route

from src.health.router import health_check


def _make_app(
    mongodb_ok=True,
    elasticsearch_ok=True,
    redis_ok=True,
    worker_alive=True,
    queue_depth=0,
    dlq_depth=0,
    queue_max_size=10000,
):
    db = AsyncMock()
    db.check_mongodb = AsyncMock(return_value=mongodb_ok)
    db.check_elasticsearch = AsyncMock(return_value=elasticsearch_ok)
    db.check_redis = AsyncMock(return_value=redis_ok)

    worker = MagicMock()
    worker.is_alive.return_value = worker_alive

    queue = AsyncMock()
    queue.qsize = AsyncMock(return_value=queue_depth)
    queue.dlq_qsize = MagicMock(return_value=dlq_depth)

    settings = MagicMock()
    settings.queue_max_size = queue_max_size

    async def endpoint(request):
        request.app.state.db = db
        request.app.state.worker = worker
        request.app.state.queue = queue
        request.app.state.settings = settings
        return await health_check(request)

    app = Starlette(routes=[Route("/health", endpoint)])
    return app


@pytest.mark.unit
class TestHealthEndpoint:
    async def test_healthy_returns_200(self):
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "healthy"

    async def test_mongodb_down_returns_503(self):
        app = _make_app(mongodb_ok=False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 503
            assert resp.json()["status"] == "unhealthy"

    async def test_worker_dead_returns_503(self):
        app = _make_app(worker_alive=False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 503
            assert resp.json()["status"] == "unhealthy"

    async def test_degraded_returns_200(self):
        app = _make_app(elasticsearch_ok=False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "degraded"
