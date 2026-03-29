import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock
from src.analytics.service import AnalyticsService, WINDOW_CONFIG
from src.analytics.schemas import TimeBucket, StatsWindow, RealtimeStatsQuery


@pytest.mark.unit
class TestPipelineBuilder:
    def test_hourly_uses_hour_unit(self):
        service = AnalyticsService(collection=AsyncMock(), cache=AsyncMock(), settings=MagicMock())
        pipeline = service.build_stats_pipeline(
            unit="hour", event_type=None, start_date=None, end_date=None,
        )
        group_stage = next(s for s in pipeline if "$group" in s)
        assert group_stage["$group"]["_id"]["time_bucket"]["$dateTrunc"]["unit"] == "hour"

    def test_daily_uses_day_unit(self):
        service = AnalyticsService(collection=AsyncMock(), cache=AsyncMock(), settings=MagicMock())
        pipeline = service.build_stats_pipeline(unit="day")
        group_stage = next(s for s in pipeline if "$group" in s)
        assert group_stage["$group"]["_id"]["time_bucket"]["$dateTrunc"]["unit"] == "day"

    def test_weekly_uses_week_unit(self):
        service = AnalyticsService(collection=AsyncMock(), cache=AsyncMock(), settings=MagicMock())
        pipeline = service.build_stats_pipeline(unit="week")
        group_stage = next(s for s in pipeline if "$group" in s)
        assert group_stage["$group"]["_id"]["time_bucket"]["$dateTrunc"]["unit"] == "week"

    def test_event_type_filter_adds_match(self):
        service = AnalyticsService(collection=AsyncMock(), cache=AsyncMock(), settings=MagicMock())
        pipeline = service.build_stats_pipeline(unit="hour", event_type="click")
        match_stage = next(s for s in pipeline if "$match" in s)
        assert match_stage["$match"]["event_type"] == "click"

    def test_date_range_adds_match(self):
        service = AnalyticsService(collection=AsyncMock(), cache=AsyncMock(), settings=MagicMock())
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=6)
        pipeline = service.build_stats_pipeline(unit="hour", start_date=start, end_date=now)
        match_stage = next(s for s in pipeline if "$match" in s)
        assert "$gte" in match_stage["$match"]["timestamp"]
        assert "$lte" in match_stage["$match"]["timestamp"]

    def test_pipeline_sorts_by_time(self):
        service = AnalyticsService(collection=AsyncMock(), cache=AsyncMock(), settings=MagicMock())
        pipeline = service.build_stats_pipeline(unit="hour")
        sort_stage = next(s for s in pipeline if "$sort" in s)
        assert "_id.time_bucket" in sort_stage["$sort"]


@pytest.mark.unit
class TestWindowConfig:
    def test_1h_maps_to_minute(self):
        assert WINDOW_CONFIG["1h"]["unit"] == "minute"

    def test_6h_maps_to_hour(self):
        assert WINDOW_CONFIG["6h"]["unit"] == "hour"

    def test_24h_maps_to_hour(self):
        assert WINDOW_CONFIG["24h"]["unit"] == "hour"


@pytest.mark.unit
class TestRealtimeStats:
    async def test_cache_hit_returns_cached(self):
        cache = AsyncMock()
        cache.get = AsyncMock(return_value=[{"count": 5}])
        service = AnalyticsService(collection=AsyncMock(), cache=cache, settings=MagicMock())
        result = await service.get_realtime_stats(RealtimeStatsQuery())
        assert result == [{"count": 5}]
        # MongoDB should NOT be called
        service._collection.aggregate.assert_not_called()

    async def test_cache_miss_runs_aggregation(self):
        cache = AsyncMock()
        cache.get = AsyncMock(return_value=None)
        cache.set = AsyncMock()

        mock_cursor = AsyncMock()
        mock_cursor.to_list = AsyncMock(return_value=[{"count": 10}])
        collection = AsyncMock()
        collection.aggregate = MagicMock(return_value=mock_cursor)

        service = AnalyticsService(collection=collection, cache=cache, settings=MagicMock(realtime_stats_ttl=30))
        result = await service.get_realtime_stats(RealtimeStatsQuery())

        assert result == [{"count": 10}]
        cache.set.assert_called_once()
