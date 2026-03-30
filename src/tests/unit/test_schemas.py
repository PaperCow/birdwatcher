import pytest
from datetime import datetime, timezone, timedelta
from src.config import get_settings
from src.events.schemas import EventCreate, EventAccepted, EventResponse, EventFilter

@pytest.mark.unit
class TestEventCreate:
    def _valid_kwargs(self, **overrides):
        base = dict(
            idempotency_key="test-123",
            event_type="pageview",
            timestamp=datetime.now(timezone.utc),
            user_id="user-1",
            source_url="https://example.com",
        )
        base.update(overrides)
        return base

    def test_valid_event(self):
        event = EventCreate(**self._valid_kwargs())
        assert event.idempotency_key == "test-123"
        assert event.event_type == "pageview"

    def test_valid_with_metadata(self):
        event = EventCreate(**self._valid_kwargs(metadata={"browser": "Chrome"}))
        assert event.metadata["browser"] == "Chrome"

    def test_missing_idempotency_key(self):
        kwargs = self._valid_kwargs()
        del kwargs["idempotency_key"]
        with pytest.raises(Exception):
            EventCreate(**kwargs)

    def test_metadata_too_large(self):
        with pytest.raises(ValueError, match="metadata"):
            EventCreate(**self._valid_kwargs(metadata={"data": "x" * 70000}))

    def test_metadata_too_deep(self):
        deep = {}
        current = deep
        for _ in range(15):
            current["nested"] = {}
            current = current["nested"]
        with pytest.raises(ValueError, match="depth"):
            EventCreate(**self._valid_kwargs(metadata=deep))

    def test_metadata_at_max_depth_ok(self):
        nested = {}
        current = nested
        for _ in range(9):  # 9 levels of nesting = depth 10 including root
            current["a"] = {}
            current = current["a"]
        event = EventCreate(**self._valid_kwargs(metadata=nested))
        assert event.metadata is not None

    def test_timestamp_too_far_past(self):
        ts = datetime.now(timezone.utc) - timedelta(days=60)
        with pytest.raises(ValueError, match="timestamp"):
            EventCreate(**self._valid_kwargs(timestamp=ts))

    def test_timestamp_too_far_future(self):
        ts = datetime.now(timezone.utc) + timedelta(minutes=30)
        with pytest.raises(ValueError, match="timestamp"):
            EventCreate(**self._valid_kwargs(timestamp=ts))

    def test_timestamp_within_bounds(self):
        ts = datetime.now(timezone.utc) - timedelta(days=5)
        event = EventCreate(**self._valid_kwargs(timestamp=ts))
        assert event.timestamp == ts


@pytest.mark.unit
class TestEventAccepted:
    def test_mirrors_input(self):
        accepted = EventAccepted(
            idempotency_key="key-1",
            event_type="click",
            timestamp=datetime.now(timezone.utc),
            user_id="u1",
            source_url="https://example.com",
            metadata={"a": 1},
        )
        assert accepted.idempotency_key == "key-1"
        assert accepted.metadata == {"a": 1}


@pytest.mark.unit
class TestEventResponse:
    def test_from_mongo(self):
        from bson import ObjectId
        oid = ObjectId()
        now = datetime.now(timezone.utc)
        doc = {
            "_id": oid,
            "idempotency_key": "key-1",
            "event_type": "pageview",
            "timestamp": now,
            "user_id": "u1",
            "source_url": "https://example.com",
            "metadata": None,
            "created_at": now,
        }
        resp = EventResponse.from_mongo(doc)
        assert resp.id == str(oid)
        assert resp.idempotency_key == "key-1"


@pytest.mark.unit
class TestEventFilter:
    def test_defaults(self):
        f = EventFilter()
        assert f.event_type is None
        assert f.skip == 0
        assert f.limit == 50

    def test_limit_bounds(self):
        with pytest.raises(Exception):
            EventFilter(limit=0)
        with pytest.raises(Exception):
            EventFilter(limit=201)


from src.analytics.schemas import TimeBucket, StatsQuery, StatsWindow, RealtimeStatsQuery, StatsBucket
from src.search.schemas import SearchQuery

@pytest.mark.unit
class TestStatsQuery:
    def test_valid(self):
        q = StatsQuery(
            time_bucket=TimeBucket.HOURLY,
            start_date=datetime.now(timezone.utc) - timedelta(days=1),
            end_date=datetime.now(timezone.utc),
        )
        assert q.time_bucket == TimeBucket.HOURLY

    def test_hourly_exceeds_max_range(self):
        with pytest.raises(ValueError, match="exceeds"):
            StatsQuery(
                time_bucket=TimeBucket.HOURLY,
                start_date=datetime.now(timezone.utc) - timedelta(days=10),
                end_date=datetime.now(timezone.utc),
            )

    def test_daily_within_range(self):
        q = StatsQuery(
            time_bucket=TimeBucket.DAILY,
            start_date=datetime.now(timezone.utc) - timedelta(days=300),
            end_date=datetime.now(timezone.utc),
        )
        assert q.time_bucket == TimeBucket.DAILY

    def test_daily_exceeds_max_range(self):
        with pytest.raises(ValueError, match="exceeds"):
            StatsQuery(
                time_bucket=TimeBucket.DAILY,
                start_date=datetime.now(timezone.utc) - timedelta(days=400),
                end_date=datetime.now(timezone.utc),
            )

    def test_no_date_range_defaults_applied(self):
        q = StatsQuery(time_bucket=TimeBucket.WEEKLY)
        assert q.start_date is not None
        assert q.end_date is not None
        span = (q.end_date - q.start_date).days
        assert span == get_settings().weekly_max_days


@pytest.mark.unit
class TestRealtimeStatsQuery:
    def test_defaults(self):
        q = RealtimeStatsQuery()
        assert q.window == StatsWindow.LAST_1H
        assert q.event_type is None

    def test_with_event_type(self):
        q = RealtimeStatsQuery(window=StatsWindow.LAST_24H, event_type="click")
        assert q.event_type == "click"


@pytest.mark.unit
class TestSearchQuery:
    def test_valid(self):
        q = SearchQuery(q="chrome mobile")
        assert q.q == "chrome mobile"
        assert q.skip == 0
        assert q.limit == 20

    def test_with_filters(self):
        q = SearchQuery(q="test", event_type="click", user_id="u1")
        assert q.event_type == "click"

    def test_limit_bounds(self):
        with pytest.raises(Exception):
            SearchQuery(q="test", limit=0)
        with pytest.raises(Exception):
            SearchQuery(q="test", limit=101)
