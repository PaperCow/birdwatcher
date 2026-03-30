import pytest
from src.queue.base import QueueMessage, ReceivedMessage
from src.queue.memory import InMemoryQueue
from src.core.exceptions import QueueFullError


def _msg(key="k1", **overrides):
    kwargs = dict(payload={"idempotency_key": key, "event_type": "click"})
    kwargs.update(overrides)
    return QueueMessage(**kwargs)


@pytest.mark.unit
class TestInMemoryQueueBasics:
    async def test_enqueue_and_drain_ordering(self):
        q = InMemoryQueue(maxsize=10, max_retries=3)
        m1, m2 = _msg("a"), _msg("b")
        await q.enqueue(m1)
        await q.enqueue(m2)
        batch = await q.drain(max_items=10, timeout=1.0)
        assert [r.queue_message.payload["idempotency_key"] for r in batch] == ["a", "b"]

    async def test_drain_returns_received_messages(self):
        q = InMemoryQueue(maxsize=10, max_retries=3)
        msg = _msg("a")
        await q.enqueue(msg)
        batch = await q.drain(max_items=10, timeout=1.0)
        assert len(batch) == 1
        assert isinstance(batch[0], ReceivedMessage)
        assert batch[0].receipt_handle == msg.message_id
        assert batch[0].queue_message is msg

    async def test_drain_respects_max_items(self):
        q = InMemoryQueue(maxsize=10, max_retries=3)
        for i in range(5):
            await q.enqueue(_msg(f"k{i}"))
        batch = await q.drain(max_items=3, timeout=1.0)
        assert len(batch) == 3

    async def test_drain_returns_empty_on_timeout(self):
        q = InMemoryQueue(maxsize=10, max_retries=3)
        batch = await q.drain(max_items=10, timeout=0.1)
        assert batch == []

    async def test_enqueue_raises_when_full(self):
        q = InMemoryQueue(maxsize=2, max_retries=3)
        await q.enqueue(_msg("a"))
        await q.enqueue(_msg("b"))
        with pytest.raises(QueueFullError):
            await q.enqueue(_msg("c"))

    async def test_qsize(self):
        q = InMemoryQueue(maxsize=10, max_retries=3)
        assert await q.qsize() == 0
        await q.enqueue(_msg())
        assert await q.qsize() == 1

    async def test_ack_removes_from_in_flight(self):
        q = InMemoryQueue(maxsize=10, max_retries=3)
        msg = _msg()
        await q.enqueue(msg)
        batch = await q.drain(max_items=1, timeout=1.0)
        assert len(q._in_flight) == 1
        await q.ack(batch[0].receipt_handle)
        assert len(q._in_flight) == 0

    async def test_drain_populates_in_flight(self):
        q = InMemoryQueue(maxsize=10, max_retries=3)
        await q.enqueue(_msg("a"))
        await q.enqueue(_msg("b"))
        batch = await q.drain(max_items=10, timeout=1.0)
        assert len(q._in_flight) == 2
        for r in batch:
            assert r.receipt_handle in q._in_flight


@pytest.mark.unit
class TestNackRouting:
    async def test_nack_increments_retry(self):
        q = InMemoryQueue(maxsize=10, max_retries=3)
        msg = _msg()
        await q.enqueue(msg)
        batch = await q.drain(max_items=1, timeout=1.0)
        await q.nack(batch[0].receipt_handle)
        assert msg.retry_count == 1

    async def test_nack_requeues(self):
        q = InMemoryQueue(maxsize=10, max_retries=3)
        msg = _msg()
        await q.enqueue(msg)
        batch = await q.drain(max_items=1, timeout=1.0)
        await q.nack(batch[0].receipt_handle)
        assert await q.qsize() == 1

    async def test_nack_removes_from_in_flight(self):
        q = InMemoryQueue(maxsize=10, max_retries=3)
        msg = _msg()
        await q.enqueue(msg)
        batch = await q.drain(max_items=1, timeout=1.0)
        assert len(q._in_flight) == 1
        await q.nack(batch[0].receipt_handle)
        assert len(q._in_flight) == 0

    async def test_nack_max_retries_routes_to_dlq(self):
        q = InMemoryQueue(maxsize=10, max_retries=2)
        msg = _msg()
        msg.retry_count = 1
        await q.enqueue(msg)
        batch = await q.drain(max_items=1, timeout=1.0)
        await q.nack(batch[0].receipt_handle)
        assert q.dlq_qsize() == 1
        assert await q.qsize() == 0

    async def test_nack_full_queue_routes_to_dlq(self):
        q = InMemoryQueue(maxsize=1, max_retries=5)
        msg = _msg("original")
        await q.enqueue(msg)
        batch = await q.drain(max_items=1, timeout=1.0)
        await q.enqueue(_msg("filler"))
        await q.nack(batch[0].receipt_handle)
        assert q.dlq_qsize() == 1

    async def test_nack_unknown_receipt_handle_is_noop(self):
        q = InMemoryQueue(maxsize=10, max_retries=3)
        await q.nack("nonexistent")  # should not raise

    async def test_ack_unknown_receipt_handle_is_noop(self):
        q = InMemoryQueue(maxsize=10, max_retries=3)
        await q.ack("nonexistent")  # should not raise


@pytest.mark.unit
class TestDlqInternal:
    async def test_dlq_qsize_starts_at_zero(self):
        q = InMemoryQueue(maxsize=10, max_retries=3)
        assert q.dlq_qsize() == 0

    async def test_dlq_receives_exhausted_messages(self):
        q = InMemoryQueue(maxsize=10, max_retries=1)
        msg = _msg()
        await q.enqueue(msg)
        batch = await q.drain(max_items=1, timeout=1.0)
        await q.nack(batch[0].receipt_handle)
        assert q.dlq_qsize() == 1
        assert await q.qsize() == 0
