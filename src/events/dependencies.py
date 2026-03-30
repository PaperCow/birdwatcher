from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import Query

from src.events.schemas import EventFilter


async def get_event_filter(
    event_type: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    source_url: Optional[str] = Query(None),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> EventFilter:
    return EventFilter(
        event_type=event_type,
        user_id=user_id,
        source_url=source_url,
        start_date=start_date,
        end_date=end_date,
        skip=skip,
        limit=limit,
    )
