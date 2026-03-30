# src/main.py
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.analytics.router import router as analytics_router
from src.analytics.service import AnalyticsService
from src.cache.middleware import RateLimitMiddleware
from src.cache.service import CacheService
from src.config import Settings, get_settings
from src.core.database import DatabaseManager
from src.core.logging import configure_logging, get_logger
from src.events.router import router as events_router
from src.events.service import EventService
from src.health.router import router as health_router
from src.ingestion.worker import EventWorker
from src.queue.dlq import DeadLetterQueue
from src.queue.memory import InMemoryQueue
from src.search.router import router as search_router
from src.search.service import SearchService

logger = get_logger(component="main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = app.state.settings

    # Use override for testing, else create fresh
    db = getattr(app.state, "db_override", None)
    if db is None:
        db = DatabaseManager(settings)
        await db.connect()

    await db.create_indexes()

    # Queue system
    dlq = DeadLetterQueue(maxsize=settings.dlq_max_size)
    queue = InMemoryQueue(maxsize=settings.queue_max_size, dlq=dlq, max_retries=settings.max_retries)

    # Services
    assert db.mongo_db is not None
    cache = CacheService(db.redis_client, default_ttl=settings.realtime_stats_ttl)
    event_service = EventService(queue=queue, collection=db.mongo_db["events"])
    analytics_service = AnalyticsService(collection=db.mongo_db["events"], cache=cache, settings=settings)
    search_service = SearchService(es_client=db.es_client, index=settings.elasticsearch_index)

    # Worker
    worker = EventWorker(
        queue=queue,
        dlq=dlq,
        mongo_collection=db.mongo_db["events"],
        es_client=db.es_client,
        es_index=settings.elasticsearch_index,
        batch_size=settings.batch_size,
        batch_timeout=settings.batch_timeout,
        max_retries=settings.max_retries,
        backoff_base=settings.backoff_base,
        backoff_max=settings.backoff_max,
    )
    worker_task = asyncio.create_task(worker.run())

    # Store on app.state
    app.state.db = db
    app.state.queue = queue
    app.state.dlq = dlq
    app.state.event_service = event_service
    app.state.analytics_service = analytics_service
    app.state.search_service = search_service
    app.state.worker = worker
    app.state.worker_task = worker_task
    # Expose redis_client for RateLimitMiddleware (it reads app.state.redis_client)
    app.state.redis_client = db.redis_client

    logger.info("startup_complete")

    yield

    # Shutdown
    logger.info("shutdown_started")
    await queue.shutdown()
    try:
        await asyncio.wait_for(worker_task, timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("worker_shutdown_timeout")
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

    await db.close()
    logger.info("shutdown_complete")


def create_app(settings: Settings | None = None, db_manager: DatabaseManager | None = None) -> FastAPI:
    if settings is None:
        settings = get_settings()

    application = FastAPI(title="Birdwatcher", version="0.1.0", lifespan=lifespan)
    application.state.settings = settings

    if db_manager is not None:
        application.state.db_override = db_manager

    # Routers
    application.include_router(events_router)
    application.include_router(analytics_router)
    application.include_router(search_router)
    application.include_router(health_router)

    # Middleware (FastAPI's add_middleware passes app automatically as first arg)
    application.add_middleware(
        RateLimitMiddleware,
        max_requests=settings.rate_limit_requests,
        window=settings.rate_limit_window,
    )

    return application


app = create_app()
