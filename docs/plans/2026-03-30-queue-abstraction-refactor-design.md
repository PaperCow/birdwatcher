# Queue Abstraction Refactor Design

Goal: Refactor the in-memory queue and its consumers to make migrating to SQS straightforward. Target is Standard SQS queues (not FIFO) — the application already handles deduplication idempotently at the MongoDB layer.

Approach: Protocol-first. Redesign the `EventQueue` protocol to match SQS-compatible semantics, update `InMemoryQueue` to conform, then update all consumers top-down.

## 1. New EventQueue Protocol

```python
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

Changes from current protocol:
- `drain` returns `list[ReceivedMessage]` — pairs a `QueueMessage` with an opaque `receipt_handle`
- `ack` and `nack` take `receipt_handle` instead of `message_id` or full message object
- `nack` no longer takes an error string — error tracking is an application concern
- `qsize` becomes async (SQS equivalent is an API call)
- No `shutdown()`, `join()`, or sentinel in the protocol — shutdown is a consumer lifecycle concern

The in-memory implementation sets `receipt_handle` to `message_id`. SQS would use the actual SQS receipt handle. The worker treats it as opaque.

## 2. InMemoryQueue Changes

**Enqueue** — raises `QueueFullError` directly instead of leaking `asyncio.QueueFull`. Exception translation moves from EventService into the queue.

**Drain** — wraps each dequeued `QueueMessage` in a `ReceivedMessage`. Populates an internal `_in_flight: dict[str, QueueMessage]` map keyed by receipt handle. SENTINEL handling stays internal — sets a shutdown flag, not included in returned list.

**In-flight tracking** — new internal state. When `drain` pops a message, it stores it in `_in_flight`. When `ack`/`nack` is called, the queue looks up the message by receipt handle. This mirrors SQS's internal tracking. Cost: at most `batch_size` entries (default 100).

**Ack** — looks up receipt handle in `_in_flight`, calls `task_done()`, removes entry.

**Nack** — looks up receipt handle in `_in_flight`, increments retry count, re-enqueues if under max retries, routes to internal DLQ if exhausted. Calls `task_done()`, removes from `_in_flight`.

**DLQ** — internal implementation detail, not exposed via protocol. `InMemoryQueue` creates and manages its own DLQ. Exposes `dlq_qsize()` as a concrete method (not on protocol) for health checks.

**Shutdown** — `shutdown()` and `join()` remain as concrete methods on `InMemoryQueue`, not on the protocol.

**qsize** — wraps `self._queue.qsize()` in an async method.

## 3. EventService Changes

Remove `asyncio.QueueFull` catch. `InMemoryQueue.enqueue` now raises `QueueFullError` directly, which propagates through the service to the router unchanged. The service no longer imports or knows about asyncio internals.

## 4. EventWorker Changes

**Ack/nack** — passes `receipt_handle` string instead of message_id or full message object. Iterates over `ReceivedMessage` objects from drain, accesses `.queue_message` for payload data.

**DLQ removal** — worker no longer routes permanent failures to DLQ. For permanent ES failures (4xx), the worker acks the message and logs the failure. The queue's internal DLQ handles retry exhaustion from transient nacks.

**Shutdown** — worker manages its own shutdown via an `asyncio.Event` instead of checking for SENTINEL in drain results. `main.py` sets the event, worker checks it at the top of each loop iteration.

**Error tracking** — worker logs errors before nacking rather than passing error strings to nack.

No changes to batch processing logic, MongoDB bulk writes, ES indexing, backoff, or heartbeat.

## 5. main.py Lifecycle Changes

**Startup:**
- DLQ no longer created separately — `InMemoryQueue` creates its own internally
- DLQ no longer passed to `EventWorker`
- Worker receives a `shutdown_event: asyncio.Event`

**Shutdown sequence:**
1. `shutdown_event.set()` — tell worker to stop polling
2. `await asyncio.wait_for(worker_task, timeout=10.0)` — wait for current batch
3. `await queue.shutdown()` — flush queue internals
4. `await db.close()`

**Health check access** — `app.state.dlq` removed. Health check gets DLQ depth through `queue.dlq_qsize()` on the concrete `InMemoryQueue`.

## 6. Health Check Changes

- `await queue.qsize()` instead of `queue.qsize()` (now async)
- `queue.dlq_qsize()` instead of `dlq.qsize()` (from queue, not separate object)
- Worker liveness and status logic unchanged

## 7. Test Changes

**InMemoryQueue unit tests:**
- Drain returns `ReceivedMessage` — tests unwrap `.queue_message` and `.receipt_handle`
- Ack/nack called with `receipt_handle` string
- DLQ tests assert via `queue.dlq_qsize()` instead of direct DLQ access
- Full test asserts `QueueFullError` instead of `asyncio.QueueFull`
- qsize calls awaited
- SENTINEL tests replaced with shutdown method tests

**EventWorker unit tests:**
- Mock queue returns `ReceivedMessage` objects
- Ack/nack assertions check receipt_handle argument
- DLQ routing tests removed from worker
- Permanent ES failure: assert ack + log instead of DLQ route + ack
- Shutdown tests use `asyncio.Event` instead of SENTINEL

**EventService unit tests:**
- Queue mock raises `QueueFullError` directly instead of `asyncio.QueueFull`

**Integration tests** — minimal changes, end-to-end flow is the same.

## 8. sqs-migration.md Document

A separate document at `docs/sqs-migration.md` covering:

1. **How the in-memory queue differs from SQS** — persistence, backpressure, retry mechanism, DLQ routing, ordering, deduplication, batch limits
2. **What the in-memory queue guarantees and doesn't** — FIFO ordering and exactly-once delivery (guarantees); persistence, multi-consumer safety, crash durability (not guaranteed)
3. **Step-by-step migration guide** — aioboto3 dependency, `SQSQueue` class implementing `EventQueue` protocol, method mapping (enqueue->SendMessage, drain->ReceiveMessage loop, ack->DeleteMessage, nack->ChangeMessageVisibility or no-op, qsize->GetQueueAttributes), SQS DLQ via redrive policy, config changes, health check updates, shutdown simplification, note on drain batching internally handling SQS's 10-per-receive limit, testing with localstack or moto
