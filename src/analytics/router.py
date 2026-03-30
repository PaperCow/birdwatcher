from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Query, Request

from src.analytics.schemas import (
    RealtimeStatsQuery,
    StatsQuery,
    StatsWindow,
    TimeBucket,
)

router = APIRouter()


@router.get("/events/stats")
async def get_stats(
    request: Request,
    time_bucket: TimeBucket = Query(...),
    event_type: Optional[str] = Query(None),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
) -> dict[str, Any]:
    query = StatsQuery(
        time_bucket=time_bucket,
        event_type=event_type,
        start_date=start_date,
        end_date=end_date,
    )
    result = await request.app.state.analytics_service.get_stats(query)
    return {"buckets": result}


@router.get("/events/stats/realtime")
async def get_realtime_stats(
    request: Request,
    window: StatsWindow = Query(StatsWindow.LAST_1H),
    event_type: Optional[str] = Query(None),
) -> dict[str, Any]:
    query = RealtimeStatsQuery(
        window=window,
        event_type=event_type,
    )
    result = await request.app.state.analytics_service.get_realtime_stats(query)
    return {"buckets": result}
