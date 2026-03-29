from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from src.analytics.schemas import (
    RealtimeStatsQuery,
    StatsQuery,
    TimeBucket,
)
from src.core.logging import get_logger

logger = get_logger(component="analytics")

WINDOW_CONFIG: dict[str, dict] = {
    "1h": {"unit": "minute", "duration": timedelta(hours=1)},
    "6h": {"unit": "hour", "duration": timedelta(hours=6)},
    "24h": {"unit": "hour", "duration": timedelta(hours=24)},
}

BUCKET_UNIT_MAP: dict[TimeBucket, str] = {
    TimeBucket.HOURLY: "hour",
    TimeBucket.DAILY: "day",
    TimeBucket.WEEKLY: "week",
}


class AnalyticsService:
    def __init__(self, collection, cache, settings) -> None:
        self._collection = collection
        self._cache = cache
        self._settings = settings

    def build_stats_pipeline(
        self,
        unit: str,
        event_type: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> list[dict]:
        pipeline: list[dict] = []

        # Optional $match stage for filters
        match_filters: dict = {}
        if event_type is not None:
            match_filters["event_type"] = event_type
        if start_date is not None or end_date is not None:
            ts_filter: dict = {}
            if start_date is not None:
                ts_filter["$gte"] = start_date
            if end_date is not None:
                ts_filter["$lte"] = end_date
            match_filters["timestamp"] = ts_filter

        if match_filters:
            pipeline.append({"$match": match_filters})

        # $group stage with $dateTrunc bucketing
        pipeline.append(
            {
                "$group": {
                    "_id": {
                        "time_bucket": {
                            "$dateTrunc": {
                                "date": "$timestamp",
                                "unit": unit,
                            }
                        },
                        "event_type": "$event_type",
                    },
                    "count": {"$sum": 1},
                }
            }
        )

        # $sort by time bucket ascending
        pipeline.append({"$sort": {"_id.time_bucket": 1}})

        # $project to reshape output
        pipeline.append(
            {
                "$project": {
                    "_id": 0,
                    "time_bucket": "$_id.time_bucket",
                    "event_type": "$_id.event_type",
                    "count": 1,
                }
            }
        )

        return pipeline

    async def get_stats(self, query: StatsQuery) -> list[dict]:
        unit = BUCKET_UNIT_MAP[query.time_bucket]
        pipeline = self.build_stats_pipeline(
            unit=unit,
            event_type=query.event_type,
            start_date=query.start_date,
            end_date=query.end_date,
        )
        cursor = self._collection.aggregate(pipeline)
        return await cursor.to_list(None)

    async def get_realtime_stats(self, query: RealtimeStatsQuery) -> list[dict]:
        window = query.window.value
        event_type = query.event_type

        cache_key = f"stats:realtime:{window}:{event_type or 'all'}"

        cached = await self._cache.get(cache_key)
        if cached is not None:
            return cached

        config = WINDOW_CONFIG[window]
        start_date = datetime.now(timezone.utc) - config["duration"]

        pipeline = self.build_stats_pipeline(
            unit=config["unit"],
            event_type=event_type,
            start_date=start_date,
        )

        cursor = self._collection.aggregate(pipeline)
        result = await cursor.to_list(None)

        await self._cache.set(cache_key, result, ttl=self._settings.realtime_stats_ttl)

        return result
