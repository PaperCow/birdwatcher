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
