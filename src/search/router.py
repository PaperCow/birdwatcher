from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Query, Request

from src.search.schemas import SearchQuery

router = APIRouter()


@router.get("/events/search")
async def search_events(
    request: Request,
    q: str = Query(...),
    event_type: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query = SearchQuery(
        q=q,
        event_type=event_type,
        user_id=user_id,
        start_date=start_date,
        end_date=end_date,
        skip=skip,
        limit=limit,
    )
    return await request.app.state.search_service.search(query)
