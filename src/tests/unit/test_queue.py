import asyncio
import pytest
from src.queue.base import QueueMessage
from src.queue.memory import InMemoryQueue, SENTINEL
from src.queue.dlq import DeadLetterQueue

def _msg(key="k1", **overrides):
    kwargs = dict(payload={"idempotency_key": key, "event_type": "click"})
    kwargs.update(overrides)
    return QueueMessage(**kwargs)

@pytest.mark.unit
class TestInMemoryQueueBasics:
    async def test_enqueue_and_drain_ordering(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=10, dlq=dlq, max_retries=3)
        m1, m2 = _msg("a"), _msg("b")
        await q.enqueue(m1)
        await q.enqueue(m2)
        batch = await q.drain(max_items=10, timeout=1.0)
        assert [m.payload["idempotency_key"] for m in batch] == ["a", "b"]

    async def test_drain_respects_max_items(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=10, dlq=dlq, max_retries=3)
        for i in range(5):
            await q.enqueue(_msg(f"k{i}"))
        batch = await q.drain(max_items=3, timeout=1.0)
        assert len(batch) == 3

    async def test_drain_returns_empty_on_timeout(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=10, dlq=dlq, max_retries=3)
        batch = await q.drain(max_items=10, timeout=0.1)
        assert batch == []

    async def test_enqueue_raises_when_full(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=2, dlq=dlq, max_retries=3)
        await q.enqueue(_msg("a"))
        await q.enqueue(_msg("b"))
        with pytest.raises(asyncio.QueueFull):
            await q.enqueue(_msg("c"))

    async def test_qsize(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=10, dlq=dlq, max_retries=3)
        assert q.qsize() == 0
        await q.enqueue(_msg())
        assert q.qsize() == 1

    async def test_sentinel_in_drain(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=10, dlq=dlq, max_retries=3)
        await q.enqueue(_msg("before"))
        await q.shutdown()
        batch = await q.drain(max_items=10, timeout=1.0)
        # batch contains the message then the sentinel
        assert batch[0].payload["idempotency_key"] == "before"
        assert batch[1] is SENTINEL

    async def test_ack_calls_task_done(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=10, dlq=dlq, max_retries=3)
        msg = _msg()
        await q.enqueue(msg)
        await q.drain(max_items=1, timeout=1.0)
        await q.ack(msg.message_id)
        # join() should return immediately since all tasks are done
        await asyncio.wait_for(q.join(), timeout=1.0)


@pytest.mark.unit
class TestDeadLetterQueue:
    async def test_put_and_get(self):
        dlq = DeadLetterQueue(maxsize=10)
        msg = _msg()
        await dlq.put(msg)
        assert dlq.qsize() == 1
        got = await dlq.get()
        assert got.message_id == msg.message_id

    async def test_overflow_drops_message(self, capsys):
        dlq = DeadLetterQueue(maxsize=1)
        await dlq.put(_msg("first"))
        await dlq.put(_msg("dropped"))  # should log, not raise
        assert dlq.qsize() == 1


@pytest.mark.unit
class TestNackRouting:
    async def test_nack_increments_retry(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=10, dlq=dlq, max_retries=3)
        msg = _msg()
        await q.enqueue(msg)
        await q.drain(max_items=1, timeout=1.0)
        await q.nack(msg, "err")
        assert msg.retry_count == 1
        assert msg.error_history == ["err"]

    async def test_nack_requeues(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=10, dlq=dlq, max_retries=3)
        msg = _msg()
        await q.enqueue(msg)
        await q.drain(max_items=1, timeout=1.0)
        await q.nack(msg, "err")
        assert q.qsize() == 1  # back in queue

    async def test_nack_max_retries_routes_to_dlq(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=10, dlq=dlq, max_retries=2)
        msg = _msg()
        msg.retry_count = 1  # one retry already
        await q.enqueue(msg)
        await q.drain(max_items=1, timeout=1.0)
        await q.nack(msg, "final-err")
        assert dlq.qsize() == 1
        assert q.qsize() == 0

    async def test_nack_full_queue_routes_to_dlq(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=1, dlq=dlq, max_retries=5)
        msg = _msg("original")
        await q.enqueue(msg)
        await q.drain(max_items=1, timeout=1.0)
        # Fill queue so nack can't re-enqueue
        await q.enqueue(_msg("filler"))
        await q.nack(msg, "err")
        assert dlq.qsize() == 1  # routed to DLQ
