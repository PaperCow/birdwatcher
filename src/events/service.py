from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from src.core.exceptions import QueueFullError
from src.events.schemas import EventCreate, EventFilter
from src.queue.base import QueueMessage


class EventService:
    """Bridges the API layer with the event queue and MongoDB collection."""

    def __init__(self, queue: Any, collection: Any) -> None:
        self._queue = queue
        self._collection = collection

    async def enqueue_event(self, event: EventCreate) -> QueueMessage:
        """Convert an EventCreate into a QueueMessage and enqueue it.

        Raises QueueFullError if the underlying queue is at capacity.
        """
        payload = event.model_dump()
        payload["created_at"] = datetime.now(timezone.utc)
        message = QueueMessage(payload=payload)
        try:
            await self._queue.enqueue(message)
        except asyncio.QueueFull:
            raise QueueFullError("Event queue is at capacity")
        return message

    async def query_events(self, filters: EventFilter) -> list[dict[str, Any]]:
        """Query events from MongoDB using the provided filters."""
        query: dict[str, Any] = {}

        if filters.event_type is not None:
            query["event_type"] = filters.event_type
        if filters.user_id is not None:
            query["user_id"] = filters.user_id
        if filters.source_url is not None:
            query["source_url"] = filters.source_url

        if filters.start_date is not None or filters.end_date is not None:
            ts_filter: dict[str, Any] = {}
            if filters.start_date is not None:
                ts_filter["$gte"] = filters.start_date
            if filters.end_date is not None:
                ts_filter["$lte"] = filters.end_date
            query["timestamp"] = ts_filter

        cursor = self._collection.find(query)
        cursor = cursor.skip(filters.skip)
        cursor = cursor.limit(filters.limit)
        return await cursor.to_list(length=filters.limit)
