# Queue Abstraction Refactor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Refactor the EventQueue protocol and its consumers to align with SQS semantics, making future SQS migration a drop-in implementation swap.

**Architecture:** Redesign the EventQueue protocol (receipt handles, async qsize, no DLQ exposure), update InMemoryQueue with in-flight tracking, update worker to use shutdown event instead of SENTINEL, move exception translation into the queue, and write an SQS migration guide.

**Tech Stack:** Python 3.13+, asyncio, FastAPI, pytest

**Design doc:** `docs/plans/2026-03-30-queue-abstraction-refactor-design.md`

---

### Task 1: Update EventQueue Protocol and Add ReceivedMessage

**Files:**
- Modify: `src/queue/base.py`

**Step 1: Write the updated protocol**

Replace the entire contents of `src/queue/base.py` with:

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
import uuid


@dataclass
class QueueMessage:
    payload: dict[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    retry_count: int = 0
    enqueued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_error: str | None = None
    error_history: list[str] = field(default_factory=list)


@dataclass
class ReceivedMessage:
    queue_message: QueueMessage
    receipt_handle: str


class EventQueue(Protocol):
    async def enqueue(self, message: QueueMessage) -> None: ...
    async def drain(self, max_items: int, timeout: float) -> list[ReceivedMessage]: ...
    async def ack(self, receipt_handle: str) -> None: ...
    async def nack(self, receipt_handle: str) -> None: ...
    async def qsize(self) -> int: ...
```

**Step 2: Run type checker to verify protocol is valid**

Run: `uv run pyright src/queue/base.py`
Expected: No errors (protocol is just a definition, nothing depends on the new shape yet)

**Step 3: Commit**

```bash
git add src/queue/base.py
git commit -m "refactor: update EventQueue protocol with receipt handles and async qsize"
```

---

### Task 2: Rewrite InMemoryQueue to Conform to New Protocol

**Files:**
- Modify: `src/queue/memory.py`
- Reference: `src/queue/dlq.py` (no changes to DLQ itself)

**Step 1: Write the failing test for the new enqueue behavior (QueueFullError instead of asyncio.QueueFull)**

In `src/tests/unit/test_queue.py`, update the test:

```python
# Replace the import line at top:
# OLD: from src.queue.memory import InMemoryQueue, SENTINEL
# NEW:
from src.queue.memory import InMemoryQueue

# Also add:
from src.queue.base import QueueMessage, ReceivedMessage
from src.core.exceptions import QueueFullError
```

Replace `test_enqueue_raises_when_full`:

```python
async def test_enqueue_raises_when_full(self):
    q = InMemoryQueue(maxsize=2, max_retries=3)
    await q.enqueue(_msg("a"))
    await q.enqueue(_msg("b"))
    with pytest.raises(QueueFullError):
        await q.enqueue(_msg("c"))
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest src/tests/unit/test_queue.py::TestInMemoryQueueBasics::test_enqueue_raises_when_full -v`
Expected: FAIL (InMemoryQueue still raises asyncio.QueueFull, constructor still requires dlq)

**Step 3: Rewrite InMemoryQueue**

Replace the entire contents of `src/queue/memory.py` with:

```python
from __future__ import annotations
import asyncio
from src.queue.base import QueueMessage, ReceivedMessage
from src.queue.dlq import DeadLetterQueue
from src.core.exceptions import QueueFullError
from src.core.logging import get_logger

logger = get_logger(component="queue")

SENTINEL = object()


class InMemoryQueue:
    def __init__(self, maxsize: int, max_retries: int = 5, dlq_maxsize: int = 1000):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._dlq = DeadLetterQueue(maxsize=dlq_maxsize)
        self._max_retries = max_retries
        self._in_flight: dict[str, QueueMessage] = {}

    async def enqueue(self, message: QueueMessage) -> None:
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            raise QueueFullError("Event queue is at capacity")

    async def drain(self, max_items: int, timeout: float) -> list[ReceivedMessage]:
        items: list[ReceivedMessage] = []
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            if first is SENTINEL:
                return items
            receipt_handle = first.message_id
            self._in_flight[receipt_handle] = first
            items.append(ReceivedMessage(queue_message=first, receipt_handle=receipt_handle))
        except asyncio.TimeoutError:
            return items

        while len(items) < max_items:
            try:
                item = self._queue.get_nowait()
                if item is SENTINEL:
                    return items
                receipt_handle = item.message_id
                self._in_flight[receipt_handle] = item
                items.append(ReceivedMessage(queue_message=item, receipt_handle=receipt_handle))
            except asyncio.QueueEmpty:
                break
        return items

    async def ack(self, receipt_handle: str) -> None:
        self._in_flight.pop(receipt_handle, None)
        self._queue.task_done()

    async def nack(self, receipt_handle: str) -> None:
        message = self._in_flight.pop(receipt_handle, None)
        if message is None:
            logger.warning("nack_unknown_receipt_handle", receipt_handle=receipt_handle)
            return
        message.retry_count += 1
        self._queue.task_done()

        if message.retry_count >= self._max_retries:
            await self._dlq.put(message)
            return
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            await self._dlq.put(message)

    async def qsize(self) -> int:
        return self._queue.qsize()

    def dlq_qsize(self) -> int:
        return self._dlq.qsize()

    async def join(self) -> None:
        await self._queue.join()

    async def shutdown(self) -> None:
        await self._queue.put(SENTINEL)
```

**Step 4: Run the single test to verify it passes**

Run: `uv run pytest src/tests/unit/test_queue.py::TestInMemoryQueueBasics::test_enqueue_raises_when_full -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/queue/memory.py
git commit -m "refactor: rewrite InMemoryQueue with receipt handles, in-flight tracking, internal DLQ"
```

---

### Task 3: Update All Queue Unit Tests

**Files:**
- Modify: `src/tests/unit/test_queue.py`

**Step 1: Rewrite the test file**

Replace the entire contents of `src/tests/unit/test_queue.py` with:

```python
import asyncio
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

    async def test_ack_calls_task_done(self):
        q = InMemoryQueue(maxsize=10, max_retries=3)
        msg = _msg()
        await q.enqueue(msg)
        batch = await q.drain(max_items=1, timeout=1.0)
        await q.ack(batch[0].receipt_handle)
        await asyncio.wait_for(q.join(), timeout=1.0)

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


@pytest.mark.unit
class TestShutdown:
    async def test_shutdown_ends_drain(self):
        q = InMemoryQueue(maxsize=10, max_retries=3)
        await q.enqueue(_msg("before"))
        await q.shutdown()
        batch = await q.drain(max_items=10, timeout=1.0)
        assert len(batch) == 1
        assert batch[0].queue_message.payload["idempotency_key"] == "before"

    async def test_shutdown_empty_queue(self):
        q = InMemoryQueue(maxsize=10, max_retries=3)
        await q.shutdown()
        batch = await q.drain(max_items=10, timeout=1.0)
        assert batch == []


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
```

**Step 2: Run all queue tests**

Run: `uv run pytest src/tests/unit/test_queue.py -v`
Expected: All tests PASS

**Step 3: Commit**

```bash
git add src/tests/unit/test_queue.py
git commit -m "test: update queue unit tests for new protocol"
```

---

### Task 4: Update EventService (Remove asyncio.QueueFull Catch)

**Files:**
- Modify: `src/events/service.py`

**Step 1: Update the test to expect QueueFullError from queue directly**

In `src/tests/unit/test_event_service.py`, update `test_enqueue_raises_queue_full`:

```python
# Replace the import:
# OLD: import asyncio
# NEW: (remove the asyncio import entirely)
from src.core.exceptions import QueueFullError

# Replace the test:
async def test_enqueue_raises_queue_full(self):
    queue = AsyncMock()
    queue.enqueue = AsyncMock(side_effect=QueueFullError("Event queue is at capacity"))
    service = EventService(queue=queue, collection=AsyncMock())
    with pytest.raises(QueueFullError):
        await service.enqueue_event(_event())
```

**Step 2: Run test to verify it passes (the service still catches asyncio.QueueFull and raises QueueFullError, so the test already passes with either side_effect)**

Run: `uv run pytest src/tests/unit/test_event_service.py::TestEventServiceEnqueue::test_enqueue_raises_queue_full -v`
Expected: PASS (the QueueFullError propagates regardless)

**Step 3: Simplify EventService**

In `src/events/service.py`, replace the `enqueue_event` method:

```python
async def enqueue_event(self, event: EventCreate) -> QueueMessage:
    """Convert an EventCreate into a QueueMessage and enqueue it.

    Raises QueueFullError if the underlying queue is at capacity.
    """
    payload = event.model_dump()
    # Set at acceptance time (not persistence time) for consistent TTL semantics
    payload["created_at"] = datetime.now(timezone.utc)
    message = QueueMessage(payload=payload)
    await self._queue.enqueue(message)
    return message
```

Also remove the `import asyncio` line from the top of the file.

**Step 4: Run all event service tests**

Run: `uv run pytest src/tests/unit/test_event_service.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/events/service.py src/tests/unit/test_event_service.py
git commit -m "refactor: remove asyncio.QueueFull catch from EventService"
```

---

### Task 5: Update EventWorker

**Files:**
- Modify: `src/ingestion/worker.py`

**Step 1: Rewrite the worker**

Replace the entire contents of `src/ingestion/worker.py` with:

```python
# src/ingestion/worker.py
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone

from pymongo.errors import BulkWriteError
from elasticsearch.helpers import async_bulk

from src.queue.base import ReceivedMessage
from src.search.service import extract_metadata_text
from src.core.logging import get_logger

logger = get_logger(component="worker")


class EventWorker:
    def __init__(
        self,
        queue,
        mongo_collection,
        es_client,
        es_index: str,
        batch_size: int,
        batch_timeout: float,
        backoff_base: float,
        backoff_max: float,
        shutdown_event: asyncio.Event,
    ):
        self._queue = queue
        self._mongo = mongo_collection
        self._es = es_client
        self._es_index = es_index
        self._batch_size = batch_size
        self._batch_timeout = batch_timeout
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._shutdown_event = shutdown_event
        self._consecutive_failures = 0
        self._last_heartbeat: datetime | None = None

    def is_alive(self, staleness_seconds: float = 30.0) -> bool:
        if self._last_heartbeat is None:
            return False
        elapsed = (datetime.now(timezone.utc) - self._last_heartbeat).total_seconds()
        return elapsed < staleness_seconds

    async def run(self) -> None:
        while True:
            try:
                while not self._shutdown_event.is_set():
                    batch = await self._queue.drain(self._batch_size, self._batch_timeout)
                    self._last_heartbeat = datetime.now(timezone.utc)

                    if batch:
                        try:
                            await self.process_batch(batch)
                            self._consecutive_failures = 0
                        except Exception as e:
                            logger.error("batch_level_failure", error=str(e))
                            for received in batch:
                                await self._queue.nack(received.receipt_handle)
                            delay = min(
                                self._backoff_base * (2 ** self._consecutive_failures),
                                self._backoff_max,
                            )
                            self._consecutive_failures += 1
                            await asyncio.sleep(delay + random.uniform(0, delay * 0.1))
                logger.info("shutdown_event_received")
                return
            except Exception:
                logger.exception("worker_fatal_error")
                delay = min(
                    self._backoff_base * (2 ** self._consecutive_failures),
                    self._backoff_max,
                )
                self._consecutive_failures += 1
                await asyncio.sleep(delay + random.uniform(0, delay * 0.1))
                logger.info("worker_restarting")

    async def process_batch(self, batch: list[ReceivedMessage]) -> None:
        # 1. MongoDB bulk write
        succeeded, failed = await self._write_mongodb(batch)

        # 2. Nack mongo failures
        for received, error_msg in failed:
            logger.warning("mongodb_item_failed", receipt_handle=received.receipt_handle, error=error_msg)
            await self._queue.nack(received.receipt_handle)

        # 3. ES bulk write for succeeded items
        if succeeded:
            await self._write_elasticsearch(succeeded)

    async def _write_mongodb(
        self, batch: list[ReceivedMessage]
    ) -> tuple[list[ReceivedMessage], list[tuple[ReceivedMessage, str]]]:
        documents = [self._to_mongo_doc(r) for r in batch]
        succeeded = list(batch)
        failed: list[tuple[ReceivedMessage, str]] = []
        try:
            await self._mongo.insert_many(documents, ordered=False)
        except BulkWriteError as e:
            error_map: dict[int, int] = {}
            for err in e.details.get("writeErrors", []):
                error_map[err["index"]] = err["code"]
            succeeded = []
            failed = []
            for i, received in enumerate(batch):
                if i not in error_map:
                    succeeded.append(received)
                elif error_map[i] == 11000:
                    succeeded.append(received)
                else:
                    failed.append((received, f"MongoDB error code {error_map[i]}"))
        return succeeded, failed

    async def _write_elasticsearch(self, batch: list[ReceivedMessage]) -> None:
        actions = [self._to_es_action(r) for r in batch]
        success_count, errors = await async_bulk(
            self._es,
            actions,
            raise_on_error=False,
            raise_on_exception=False,
        )
        if not isinstance(errors, list):
            raise TypeError(f"Expected error list from async_bulk, got {type(errors).__name__}")
        failed_map: dict[str, int] = {}
        for err_item in errors:
            action_type = next(iter(err_item))
            detail = err_item[action_type]
            failed_map[detail["_id"]] = detail["status"]

        for received in batch:
            key = received.queue_message.payload["idempotency_key"]
            if key not in failed_map:
                await self._queue.ack(received.receipt_handle)
            else:
                status = failed_map[key]
                if status == 429 or status >= 500:
                    logger.warning("es_retryable_failure", receipt_handle=received.receipt_handle, status=status)
                    await self._queue.nack(received.receipt_handle)
                else:
                    # Permanent failure: ack to remove from queue, log for observability.
                    # The message will never succeed — retrying is pointless.
                    logger.error(
                        "es_permanent_failure",
                        receipt_handle=received.receipt_handle,
                        idempotency_key=key,
                        status=status,
                    )
                    await self._queue.ack(received.receipt_handle)

    def _to_mongo_doc(self, received: ReceivedMessage) -> dict:
        p = received.queue_message.payload
        return {
            "idempotency_key": p["idempotency_key"],
            "event_type": p["event_type"],
            "timestamp": p["timestamp"],
            "user_id": p["user_id"],
            "source_url": p["source_url"],
            "metadata": p.get("metadata"),
            "created_at": p["created_at"],
        }

    def _to_es_action(self, received: ReceivedMessage) -> dict:
        p = received.queue_message.payload
        metadata = p.get("metadata") or {}
        return {
            "_index": self._es_index,
            "_id": p["idempotency_key"],
            "event_type": p["event_type"],
            "timestamp": p["timestamp"],
            "user_id": p["user_id"],
            "source_url": p["source_url"],
            "metadata": metadata,
            "metadata_text": extract_metadata_text(metadata),
        }
```

**Step 2: Run type checker**

Run: `uv run pyright src/ingestion/worker.py`
Expected: No errors (or only pre-existing ones)

**Step 3: Commit**

```bash
git add src/ingestion/worker.py
git commit -m "refactor: update EventWorker for new queue protocol and shutdown event"
```

---

### Task 6: Update Worker Unit Tests

**Files:**
- Modify: `src/tests/unit/test_worker.py`

**Step 1: Rewrite the test file**

Replace the entire contents of `src/tests/unit/test_worker.py` with:

```python
# src/tests/unit/test_worker.py
import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from pymongo.errors import BulkWriteError
from src.ingestion.worker import EventWorker
from src.queue.base import QueueMessage, ReceivedMessage


def _msg(key="k1"):
    return QueueMessage(payload={
        "idempotency_key": key,
        "event_type": "click",
        "timestamp": datetime.now(timezone.utc),
        "user_id": "u1",
        "source_url": "https://example.com",
        "metadata": {"browser": "Chrome"},
        "created_at": datetime.now(timezone.utc),
    })


def _received(key="k1"):
    msg = _msg(key)
    return ReceivedMessage(queue_message=msg, receipt_handle=msg.message_id)


def _make_worker(mongo=None, es=None, queue=None, shutdown_event=None):
    return EventWorker(
        queue=queue or AsyncMock(),
        mongo_collection=mongo or AsyncMock(),
        es_client=es or AsyncMock(),
        es_index="events",
        batch_size=100,
        batch_timeout=5.0,
        backoff_base=2.0,
        backoff_max=60.0,
        shutdown_event=shutdown_event or asyncio.Event(),
    )


@pytest.mark.unit
class TestWorkerMongoDB:
    async def test_insert_many_called_ordered_false(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        worker = _make_worker(mongo=mongo)

        batch = [_received("a"), _received("b")]
        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(2, [])):
            await worker.process_batch(batch)

        mongo.insert_many.assert_called_once()
        _, kwargs = mongo.insert_many.call_args
        assert kwargs.get("ordered") is False

    async def test_duplicate_key_treated_as_success(self):
        mongo = AsyncMock()
        error = BulkWriteError({
            "writeErrors": [{"index": 0, "code": 11000, "errmsg": "dup"}],
            "nInserted": 1,
        })
        mongo.insert_many = AsyncMock(side_effect=error)
        queue = AsyncMock()

        worker = _make_worker(mongo=mongo, queue=queue)
        batch = [_received("dup"), _received("new")]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(2, [])):
            await worker.process_batch(batch)

        assert queue.ack.call_count == 2

    async def test_non_duplicate_error_nacks(self):
        mongo = AsyncMock()
        error = BulkWriteError({
            "writeErrors": [{"index": 0, "code": 999, "errmsg": "other"}],
            "nInserted": 1,
        })
        mongo.insert_many = AsyncMock(side_effect=error)
        queue = AsyncMock()

        worker = _make_worker(mongo=mongo, queue=queue)
        batch = [_received("fail"), _received("ok")]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])):
            await worker.process_batch(batch)

        queue.nack.assert_called_once()
        nacked_handle = queue.nack.call_args[0][0]
        assert nacked_handle == batch[0].receipt_handle
        queue.ack.assert_called_once()

    async def test_only_succeeded_forwarded_to_es(self):
        mongo = AsyncMock()
        error = BulkWriteError({
            "writeErrors": [{"index": 1, "code": 999, "errmsg": "fail"}],
            "nInserted": 1,
        })
        mongo.insert_many = AsyncMock(side_effect=error)

        worker = _make_worker(mongo=mongo)
        batch = [_received("ok"), _received("fail")]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])) as mock_bulk:
            await worker.process_batch(batch)

        actions = mock_bulk.call_args[0][1]
        assert len(actions) == 1
        assert actions[0]["_id"] == "ok"


@pytest.mark.unit
class TestWorkerElasticsearch:
    async def test_es_documents_use_idempotency_key_as_id(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        worker = _make_worker(mongo=mongo)
        batch = [_received("my-key")]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])) as mock_bulk:
            await worker.process_batch(batch)

        actions = mock_bulk.call_args[0][1]
        assert actions[0]["_id"] == "my-key"

    async def test_es_documents_include_metadata_text(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        worker = _make_worker(mongo=mongo)
        batch = [_received()]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])) as mock_bulk:
            await worker.process_batch(batch)

        actions = mock_bulk.call_args[0][1]
        assert "metadata_text" in actions[0]
        assert "Chrome" in actions[0]["metadata_text"]

    async def test_es_success_acks(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        worker = _make_worker(mongo=mongo, queue=queue)
        batch = [_received()]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])):
            await worker.process_batch(batch)

        queue.ack.assert_called_once()

    async def test_es_429_nacks(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        worker = _make_worker(mongo=mongo, queue=queue)
        batch = [_received("k1")]

        errors = [{"index": {"_id": "k1", "status": 429, "error": {"type": "es_rejected"}}}]
        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(0, errors)):
            await worker.process_batch(batch)

        queue.nack.assert_called_once()

    async def test_es_500_nacks(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        worker = _make_worker(mongo=mongo, queue=queue)
        batch = [_received("k1")]

        errors = [{"index": {"_id": "k1", "status": 500, "error": {"type": "internal"}}}]
        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(0, errors)):
            await worker.process_batch(batch)

        queue.nack.assert_called_once()

    async def test_es_400_acks_and_logs(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        worker = _make_worker(mongo=mongo, queue=queue)
        batch = [_received("k1")]

        errors = [{"index": {"_id": "k1", "status": 400, "error": {"type": "mapping"}}}]
        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(0, errors)):
            await worker.process_batch(batch)

        queue.ack.assert_called_once()
        queue.nack.assert_not_called()


@pytest.mark.unit
class TestWorkerBackoff:
    async def test_batch_failure_triggers_backoff(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock(side_effect=ConnectionError("down"))
        queue = AsyncMock()
        shutdown_event = asyncio.Event()

        worker = _make_worker(mongo=mongo, queue=queue, shutdown_event=shutdown_event)

        call_count = 0
        async def drain_side_effect(max_items, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [_received()]
            shutdown_event.set()
            return []

        queue.drain = AsyncMock(side_effect=drain_side_effect)

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await worker.run()

        mock_sleep.assert_called_once()
        delay = mock_sleep.call_args[0][0]
        assert delay > 0

    async def test_consecutive_failures_increase_backoff(self):
        worker = _make_worker()
        worker._consecutive_failures = 4
        expected_base = min(2.0 * (2 ** 4), 60.0)
        assert expected_base == 32.0

    async def test_success_resets_failure_counter(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        shutdown_event = asyncio.Event()

        worker = _make_worker(mongo=mongo, queue=queue, shutdown_event=shutdown_event)
        worker._consecutive_failures = 5

        call_count = 0
        async def drain_side_effect(max_items, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [_received()]
            shutdown_event.set()
            return []

        queue.drain = AsyncMock(side_effect=drain_side_effect)

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])):
            await worker.run()

        assert worker._consecutive_failures == 0


@pytest.mark.unit
class TestWorkerShutdown:
    async def test_shutdown_event_stops_worker(self):
        queue = AsyncMock()
        shutdown_event = asyncio.Event()
        shutdown_event.set()
        queue.drain = AsyncMock(return_value=[])
        worker = _make_worker(queue=queue, shutdown_event=shutdown_event)
        await worker.run()  # should exit cleanly

    async def test_processes_batch_before_shutdown(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        shutdown_event = asyncio.Event()

        worker = _make_worker(mongo=mongo, queue=queue, shutdown_event=shutdown_event)

        call_count = 0
        async def drain_side_effect(max_items, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [_received()]
            shutdown_event.set()
            return []

        queue.drain = AsyncMock(side_effect=drain_side_effect)

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])):
            await worker.run()

        mongo.insert_many.assert_called_once()

    async def test_is_alive_heartbeat(self):
        worker = _make_worker()
        assert not worker.is_alive()
        worker._last_heartbeat = datetime.now(timezone.utc)
        assert worker.is_alive()
```

**Step 2: Run all worker tests**

Run: `uv run pytest src/tests/unit/test_worker.py -v`
Expected: All PASS

**Step 3: Commit**

```bash
git add src/tests/unit/test_worker.py
git commit -m "test: update worker unit tests for new queue protocol and shutdown event"
```

---

### Task 7: Update main.py Lifecycle

**Files:**
- Modify: `src/main.py`

**Step 1: Update the lifespan function**

In `src/main.py`, make these changes:

Remove import of `DeadLetterQueue`:
```python
# Remove this line:
from src.queue.dlq import DeadLetterQueue
```

Replace the queue system and worker setup section (lines 41-65) with:

```python
    # Queue system
    queue = InMemoryQueue(
        maxsize=settings.queue_max_size,
        max_retries=settings.max_retries,
        dlq_maxsize=settings.dlq_max_size,
    )

    # Services
    assert db.mongo_db is not None
    cache = CacheService(db.redis_client, default_ttl=settings.realtime_stats_ttl)
    event_service = EventService(queue=queue, collection=db.mongo_db["events"])
    analytics_service = AnalyticsService(collection=db.mongo_db["events"], cache=cache, settings=settings)
    search_service = SearchService(es_client=db.es_client, index=settings.elasticsearch_index)

    # Worker
    shutdown_event = asyncio.Event()
    worker = EventWorker(
        queue=queue,
        mongo_collection=db.mongo_db["events"],
        es_client=db.es_client,
        es_index=settings.elasticsearch_index,
        batch_size=settings.batch_size,
        batch_timeout=settings.batch_timeout,
        backoff_base=settings.backoff_base,
        backoff_max=settings.backoff_max,
        shutdown_event=shutdown_event,
    )
    worker_task = asyncio.create_task(worker.run())
```

Replace `app.state` assignments (lines 67-75) with:

```python
    # Store on app.state
    app.state.db = db
    app.state.queue = queue
    app.state.event_service = event_service
    app.state.analytics_service = analytics_service
    app.state.search_service = search_service
    app.state.worker = worker
    app.state.worker_task = worker_task
    app.state.redis_client = db.redis_client
```

Note: `app.state.dlq` is removed.

Replace the shutdown section (lines 84-96) with:

```python
    # Shutdown
    logger.info("shutdown_started")
    shutdown_event.set()
    try:
        await asyncio.wait_for(worker_task, timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("worker_shutdown_timeout")
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

    await db.close()
    logger.info("shutdown_complete")
```

**Step 2: Run type checker**

Run: `uv run pyright src/main.py`
Expected: No errors (or only pre-existing ones)

**Step 3: Commit**

```bash
git add src/main.py
git commit -m "refactor: update main.py lifecycle for internal DLQ and shutdown event"
```

---

### Task 8: Update Health Check

**Files:**
- Modify: `src/health/router.py`

**Step 1: Update the health check endpoint**

Replace the entire contents of `src/health/router.py` with:

```python
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/health")
async def health_check(request: Request) -> dict:
    state = request.app.state

    db = state.db
    worker = state.worker
    queue = state.queue
    settings = state.settings

    # Check dependencies concurrently
    mongodb_ok, elasticsearch_ok, redis_ok = await asyncio.gather(
        db.check_mongodb(), db.check_elasticsearch(), db.check_redis()
    )

    # Check pipeline
    worker_alive = worker.is_alive()
    queue_depth = await queue.qsize()
    dlq_depth = queue.dlq_qsize()

    # unhealthy: core function unavailable (MongoDB is source of truth, worker processes events)
    # degraded: partial feature loss (ES=search only, Redis=cache+rate limiting, DLQ/queue=pipeline pressure)
    if not mongodb_ok or not worker_alive:
        status = "unhealthy"
    elif (
        not elasticsearch_ok
        or not redis_ok
        or dlq_depth > 0
        or queue_depth > settings.queue_max_size * 0.9
    ):
        status = "degraded"
    else:
        status = "healthy"

    body = {
        "status": status,
        "dependencies": {
            "mongodb": "up" if mongodb_ok else "down",
            "elasticsearch": "up" if elasticsearch_ok else "down",
            "redis": "up" if redis_ok else "down",
        },
        "pipeline": {
            "worker_alive": worker_alive,
            "queue_depth": queue_depth,
            "dlq_depth": dlq_depth,
        },
    }
    http_status = 503 if status == "unhealthy" else 200
    return JSONResponse(content=body, status_code=http_status)
```

**Step 2: Run health integration test**

Run: `uv run pytest src/tests/integration/test_health.py -v`
Expected: PASS

**Step 3: Commit**

```bash
git add src/health/router.py
git commit -m "refactor: update health check for async qsize and internal DLQ"
```

---

### Task 9: Update Integration Test Conftest

**Files:**
- Modify: `src/tests/integration/conftest.py`

**Step 1: Update `wait_for_processing`**

The `wait_for_processing` function calls `app.state.queue.join()` which still exists on `InMemoryQueue`. No change needed for that.

However, the `app` fixture no longer stores `dlq` on `app.state`. If any integration test accesses `app.state.dlq`, it will fail. Check and update as needed.

The current integration tests don't access `app.state.dlq` directly, so no changes are needed to the conftest or integration tests.

**Step 2: Run all integration tests**

Run: `uv run pytest src/tests/integration/ -v`
Expected: All PASS

**Step 3: Commit (only if changes were needed)**

No commit needed if no changes.

---

### Task 10: Run Full Test Suite and Type Check

**Files:** None (validation only)

**Step 1: Run all unit tests**

Run: `uv run pytest src/tests/unit/ -v`
Expected: All PASS

**Step 2: Run all integration tests**

Run: `uv run pytest src/tests/integration/ -v`
Expected: All PASS

**Step 3: Run type checker on all source**

Run: `uv run pyright src/`
Expected: No new errors

**Step 4: Commit any fixups if needed**

---

### Task 11: Write SQS Migration Guide

**Files:**
- Create: `docs/sqs-migration.md`

**Step 1: Write the migration guide**

Create `docs/sqs-migration.md` with:

```markdown
# SQS Migration Guide

This document describes how the in-memory `InMemoryQueue` differs from AWS SQS,
what it guarantees and doesn't, and a step-by-step guide for migrating to SQS.

## How the In-Memory Queue Differs from SQS

| Aspect | InMemoryQueue | SQS Standard |
|---|---|---|
| **Persistence** | Lost on process restart | Durable, replicated across AZs |
| **Backpressure** | Bounded size, raises QueueFullError | Effectively unbounded (120k in-flight limit) |
| **Retry mechanism** | App-tracked retry_count via nack | SQS-tracked receive count + visibility timeout |
| **DLQ routing** | Internal, after max nacks | Native redrive policy after maxReceiveCount |
| **Ordering** | Strict FIFO | Best-effort (no ordering guarantee) |
| **Deduplication** | None at queue level | None for Standard (FIFO-only feature) |
| **Batch receive** | Configurable (default 100) | Max 10 per ReceiveMessage call |
| **Delayed retry** | Immediate re-enqueue on nack | Visibility timeout controls retry delay |
| **Multi-consumer** | Single consumer only | Multiple consumers with visibility timeout |
| **Message size** | No limit (Python memory) | 256 KB default, 1 MiB max (including attributes) |

## What the In-Memory Queue Guarantees

- **FIFO ordering**: Messages are dequeued in enqueue order
- **Exactly-once delivery**: Each message is delivered to the consumer exactly once (unless nacked)
- **Bounded memory**: Queue rejects new messages when at capacity
- **Graceful shutdown**: In-flight batch completes before process exits

## What It Does NOT Guarantee

- **Persistence**: All messages lost on crash or restart
- **Multi-consumer safety**: Only one worker should consume from the queue
- **Delivery after crash**: Messages in-flight during a crash are lost
- **Per-message retry delay**: Nacked messages are immediately re-enqueued (no backoff between retries of the same message)
- **DLQ inspection/redrive**: Internal DLQ messages cannot be inspected or redriven

## Migration Steps

### 1. Add Dependencies

Add `aioboto3` to `pyproject.toml`:

```toml
"aioboto3>=13.0.0",
```

### 2. Add SQS Configuration

Add to `src/config.py`:

```python
# SQS (set queue_backend="sqs" to use)
queue_backend: str = "memory"
sqs_queue_url: str = ""
sqs_region: str = "us-east-1"
sqs_visibility_timeout: int = 30
sqs_wait_time_seconds: int = 20
```

### 3. Create SQSQueue Implementation

Create `src/queue/sqs.py` implementing the `EventQueue` protocol:

- `enqueue(message)` → `sqs.send_message(MessageBody=json.dumps(message.payload))`
- `drain(max_items, timeout)` → Loop `sqs.receive_message(MaxNumberOfMessages=min(max_items, 10), WaitTimeSeconds=...)` until batch is full or no more messages. Return `ReceivedMessage` with SQS `ReceiptHandle`.
- `ack(receipt_handle)` → `sqs.delete_message(ReceiptHandle=receipt_handle)`
- `nack(receipt_handle)` → `sqs.change_message_visibility(ReceiptHandle=receipt_handle, VisibilityTimeout=0)` or let visibility timeout expire naturally for delayed retry
- `qsize()` → `sqs.get_queue_attributes(AttributeNames=['ApproximateNumberOfMessages'])` — returns approximate count

**Batch size note**: SQS limits `MaxNumberOfMessages` to 10 per `ReceiveMessage` call. The `drain` implementation must loop internally to fill batches larger than 10. Accumulate messages until `max_items` is reached or a receive call returns no messages.

### 4. Configure SQS DLQ

Create two SQS queues: the main queue and a DLQ.

Set a redrive policy on the main queue:

```json
{
  "deadLetterTargetArn": "arn:aws:sqs:REGION:ACCOUNT:birdwatcher-dlq",
  "maxReceiveCount": 5
}
```

SQS automatically moves messages to the DLQ after `maxReceiveCount` receives without deletion. This replaces the application-level retry counting in `InMemoryQueue.nack()`.

### 5. Update main.py

Select queue implementation based on config:

```python
if settings.queue_backend == "sqs":
    queue = SQSQueue(
        queue_url=settings.sqs_queue_url,
        region=settings.sqs_region,
        visibility_timeout=settings.sqs_visibility_timeout,
        wait_time_seconds=settings.sqs_wait_time_seconds,
    )
else:
    queue = InMemoryQueue(
        maxsize=settings.queue_max_size,
        max_retries=settings.max_retries,
        dlq_maxsize=settings.dlq_max_size,
    )
```

The worker, event service, and health check work unchanged — they only depend on the `EventQueue` protocol.

### 6. Update Health Check for SQS DLQ

The health check calls `queue.dlq_qsize()` which is a concrete method on `InMemoryQueue`, not on the protocol. For SQS:

- Add a `dlq_qsize()` method to `SQSQueue` that calls `get_queue_attributes` on the DLQ URL
- Or create a shared interface for queue monitoring that both implementations satisfy

### 7. Simplify Shutdown

With SQS, shutdown is simpler:

- Set the shutdown event (worker stops polling)
- Wait for the current batch to finish processing
- No need to call `queue.shutdown()` or `queue.join()` — SQS messages that weren't acked will become visible again after the visibility timeout

### 8. Handle Backpressure Differences

SQS does not have a concept of "queue full" for producers. The `QueueFullError` path in the API (HTTP 503) won't trigger with SQS unless you implement application-level backpressure (e.g., checking approximate queue depth before enqueuing).

Options:
- Remove backpressure entirely (SQS can handle very high volume)
- Check `ApproximateNumberOfMessages` before enqueuing and reject if above a threshold
- Rely on rate limiting middleware for inbound request throttling

### 9. Testing

Use [moto](https://github.com/getmoto/moto) or [localstack](https://localstack.cloud/) for SQS integration tests. Both support the SQS API locally without AWS credentials.

```python
# Example with moto
from moto import mock_aws

@mock_aws
async def test_sqs_queue_enqueue():
    # Create mock SQS queue
    # Test enqueue/drain/ack/nack
    ...
```
```

**Step 2: Commit**

```bash
git add docs/sqs-migration.md
git commit -m "docs: add SQS migration guide"
```

---

### Task 12: Final Cleanup

**Files:**
- Check: `src/queue/dlq.py` — still used internally by InMemoryQueue, no changes needed
- Check: `src/events/router.py` — still catches QueueFullError, no changes needed

**Step 1: Verify no remaining references to old patterns**

Search for these patterns that should no longer exist outside of `src/queue/memory.py` and `src/queue/dlq.py`:
- `asyncio.QueueFull` in service/router code
- `SENTINEL` in worker code
- `dlq` parameter in worker constructor
- `app.state.dlq` in any code outside tests

Run:
```bash
rg "asyncio\.QueueFull" src/ --glob '!src/queue/*' --glob '!src/tests/*'
rg "SENTINEL" src/ --glob '!src/queue/*'
rg "app\.state\.dlq" src/
```

Expected: No matches

**Step 2: Run full test suite one final time**

Run: `uv run pytest src/tests/ -v`
Expected: All PASS

**Step 3: Commit any remaining fixups**

```bash
git add -A
git commit -m "refactor: queue abstraction refactor complete"
```
