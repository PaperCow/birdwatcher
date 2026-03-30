from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/health")
async def health_check(request: Request) -> dict:
    state = request.app.state

    db = state.db
    worker = state.worker
    queue = state.queue
    settings = state.settings

    # Check dependencies concurrently
    mongodb_ok, elasticsearch_ok, redis_ok = await asyncio.gather(
        db.check_mongodb(), db.check_elasticsearch(), db.check_redis()
    )

    # Check pipeline
    worker_alive = worker.is_alive()
    queue_depth = await queue.qsize()
    dlq_depth = queue.dlq_qsize()

    # unhealthy: core function unavailable (MongoDB is source of truth, worker processes events)
    # degraded: partial feature loss (ES=search only, Redis=cache+rate limiting, DLQ/queue=pipeline pressure)
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

    body = {
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
    http_status = 503 if status == "unhealthy" else 200
    return JSONResponse(content=body, status_code=http_status)
