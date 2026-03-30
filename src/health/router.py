from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
async def health_check(request: Request) -> dict:
    state = request.app.state

    db = state.db
    worker = state.worker
    queue = state.queue
    dlq = state.dlq
    settings = state.settings

    # Check dependencies
    mongodb_ok = await db.check_mongodb()
    elasticsearch_ok = await db.check_elasticsearch()
    redis_ok = await db.check_redis()

    # Check pipeline
    worker_alive = worker.is_alive()
    queue_depth = queue.qsize()
    dlq_depth = dlq.qsize()

    # Status logic
    if not mongodb_ok or not worker_alive:
        status = "unhealthy"
    elif (
        not elasticsearch_ok
        or not redis_ok
        or dlq_depth > 0
        or queue_depth > settings.queue_max_size * 0.9
    ):
        status = "degraded"
    else:
        status = "healthy"

    return {
        "status": status,
        "dependencies": {
            "mongodb": "up" if mongodb_ok else "down",
            "elasticsearch": "up" if elasticsearch_ok else "down",
            "redis": "up" if redis_ok else "down",
        },
        "pipeline": {
            "worker_alive": worker_alive,
            "queue_depth": queue_depth,
            "dlq_depth": dlq_depth,
        },
    }
