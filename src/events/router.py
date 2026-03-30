from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from src.core.exceptions import QueueFullError
from src.events.dependencies import get_event_filter
from src.events.schemas import EventAccepted, EventCreate, EventFilter, EventResponse

router = APIRouter()


@router.post("/events", status_code=202)
async def create_event(event: EventCreate, request: Request) -> EventAccepted:
    try:
        await request.app.state.event_service.enqueue_event(event)
    except QueueFullError:
        return JSONResponse(
            status_code=503,
            content={"detail": "Event queue is at capacity"},
        )
    return EventAccepted(**event.model_dump())


@router.get("/events")
async def list_events(
    request: Request,
    filters: EventFilter = Depends(get_event_filter),
) -> list[EventResponse]:
    results = await request.app.state.event_service.query_events(filters)
    return [EventResponse.from_mongo(doc) for doc in results]
