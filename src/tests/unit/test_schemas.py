import pytest
from datetime import datetime, timezone, timedelta
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
