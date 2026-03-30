import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from src.events.service import EventService
from src.events.schemas import EventCreate, EventFilter
from src.core.exceptions import QueueFullError

def _event(**overrides):
    base = dict(
        idempotency_key="key-1",
        event_type="pageview",
        timestamp=datetime.now(timezone.utc),
        user_id="user-1",
        source_url="https://example.com",
    )
    base.update(overrides)
    return EventCreate(**base)

@pytest.mark.unit
class TestEventServiceEnqueue:
    async def test_enqueue_creates_message(self):
        queue = AsyncMock()
        queue.enqueue = AsyncMock()
        service = EventService(queue=queue, collection=AsyncMock())
        await service.enqueue_event(_event())
        queue.enqueue.assert_called_once()
        msg = queue.enqueue.call_args[0][0]
        assert msg.payload["idempotency_key"] == "key-1"
        assert "created_at" in msg.payload

    async def test_enqueue_sets_created_at(self):
        queue = AsyncMock()
        queue.enqueue = AsyncMock()
        service = EventService(queue=queue, collection=AsyncMock())
        before = datetime.now(timezone.utc)
        await service.enqueue_event(_event())
        msg = queue.enqueue.call_args[0][0]
        assert msg.payload["created_at"] >= before

    async def test_enqueue_raises_queue_full(self):
        queue = AsyncMock()
        queue.enqueue = AsyncMock(side_effect=QueueFullError("Event queue is at capacity"))
        service = EventService(queue=queue, collection=AsyncMock())
        with pytest.raises(QueueFullError):
            await service.enqueue_event(_event())


@pytest.mark.unit
class TestEventServiceQuery:
    async def test_query_builds_filter(self):
        mock_cursor = AsyncMock()
        mock_cursor.to_list = AsyncMock(return_value=[])
        mock_cursor.sort = MagicMock(return_value=mock_cursor)
        mock_cursor.skip = MagicMock(return_value=mock_cursor)
        mock_cursor.limit = MagicMock(return_value=mock_cursor)

        collection = AsyncMock()
        collection.find = MagicMock(return_value=mock_cursor)

        service = EventService(queue=AsyncMock(), collection=collection)
        filters = EventFilter(event_type="click", user_id="u1")
        await service.query_events(filters)

        call_args = collection.find.call_args[0][0]
        assert call_args["event_type"] == "click"
        assert call_args["user_id"] == "u1"

    async def test_query_applies_pagination(self):
        mock_cursor = AsyncMock()
        mock_cursor.to_list = AsyncMock(return_value=[])
        mock_cursor.sort = MagicMock(return_value=mock_cursor)
        mock_cursor.skip = MagicMock(return_value=mock_cursor)
        mock_cursor.limit = MagicMock(return_value=mock_cursor)

        collection = AsyncMock()
        collection.find = MagicMock(return_value=mock_cursor)

        service = EventService(queue=AsyncMock(), collection=collection)
        filters = EventFilter(skip=10, limit=25)
        await service.query_events(filters)

        mock_cursor.skip.assert_called_with(10)
        mock_cursor.limit.assert_called_with(25)
