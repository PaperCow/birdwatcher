# Distributed Event Processing Platform — Design (v6)

v6: Incorporates review feedback from 2026-03-29-design-review-v5.md. See prior versions in 2026-03-29-event-platform-design-v5.md, 2026-03-29-event-platform-design-v4.md, 2026-03-29-event-platform-design-v3.md, 2026-03-29-event-platform-design-v2.md, and 2026-03-29-event-platform-design.md.

## 1. Data Flow & System Architecture

```
Client
  │
  ▼
POST /events ──▶ Validate (Pydantic) ──▶ asyncio.Queue (bounded)
  │                                            │
  ▼                                            ▼
202 Accepted                              Worker (background asyncio.Task)
(EventAccepted)                                │
                                          Batch: drain up to N items or flush on timeout
                                               │
                                               ▼
                                          MongoDB (insert_many, ordered=False)
                                          source of truth
                                               │
                                          per-item results (BulkWriteError handling)
                                               │
                                          ┌────┴────┐
                                     succeeded       failed
                                     + deduped         │
                                          │         nack / DLQ
                                          ▼
                                     Elasticsearch (async_bulk)
                                     dual write, _id = idempotency_key
                                          │
                                     per-item results (status code classification)
                                          │
                                     ┌────┴────┐
                                succeeded    failed
                                  │       ┌──┴──┐
                                 ack   retryable  permanent
                                       (429/5xx)  (4xx)
                                          │         │
                                        nack       DLQ
                                          │
                                          │  On max retries exceeded
                                          │  OR nack fails (queue full)
                                          ▼
                                      DLQ (asyncio.Queue, bounded)

GET /health ─────────────▶ Check MongoDB + ES + Redis connectivity
                           + queue depth, DLQ depth, worker liveness
GET /events ─────────────▶ MongoDB (filtered query)
GET /events/stats ───────▶ MongoDB (aggregation pipeline)
GET /events/search ──────▶ Elasticsearch (full-text search)
GET /events/stats/realtime ──▶ Redis cache ──miss──▶ MongoDB aggregation
                                                         │
                                                    cache result
```

- All writes go through the queue — the POST handler never touches MongoDB/ES directly. POST returns `202 Accepted` with an `EventAccepted` response (validated input echoed back, no persistence fields).
- MongoDB is the source of truth; Elasticsearch is a search index.
- The worker batches events for bulk writes. MongoDB is written first (`insert_many`, `ordered=False`); only items that succeeded in MongoDB (including duplicates treated as success) are forwarded to the ES bulk write.
- ES documents use `idempotency_key` as their `_id`, making ES writes idempotent — retries overwrite rather than duplicate. This also provides a stable correlation key between ES search results and the authoritative MongoDB document.
- If an item fails in MongoDB (non-duplicate), it is nacked or routed to DLQ based on retry count. If an item fails in ES, the failure is classified by status code: 429/5xx are retryable (nack), 4xx (except 429) are permanent (DLQ immediately). Nacked items re-enter the queue for retry via non-blocking `put_nowait()` — if the queue is full, the item is routed to the DLQ instead (see section 4, In-Memory Implementation). On retry, MongoDB `DuplicateKeyError` (code 11000) is treated as success — the event was already persisted, and the retry continues to the ES write (which is also idempotent via the `_id`). This is the expected retry path for ES-only retryable failures. In production, CDC via change streams eliminates this dual-write retry concern entirely.
- Redis caches the realtime stats endpoint with configurable TTL; cache misses fall through to MongoDB.
- If Redis is down, the endpoint degrades to a direct MongoDB query (no 500). The thundering herd risk of this fallback is documented in ARCHITECTURE.md with a circuit breaker as the production mitigation.

### Data Consistency: MongoDB ↔ Elasticsearch

Application-level dual write from the worker. MongoDB is written first (source of truth), then ES is written for the items that succeeded. Both writes are idempotent: MongoDB via the unique index on `idempotency_key`, ES via explicit `_id` set to `idempotency_key` (indexing with the same `_id` is an upsert). If ES fails for a given item with a retryable error (429/5xx), the item is nacked and retried through the main queue — the subsequent MongoDB `DuplicateKeyError` is treated as success, and ES gets another attempt that overwrites any partial state. If ES fails with a permanent error (4xx except 429), the item goes directly to the DLQ — retrying won't help, and the event is already persisted in MongoDB (source of truth intact). This means every ES retry incurs a redundant (but cheap) MongoDB unique index lookup.

This dual-write retry approach is acceptable for this scope. In production, CDC via MongoDB change streams would replace the dual write for decoupled, eventually-consistent sync — ES indexing is handled by the change stream consumer with resume tokens for fault recovery, eliminating the redundant MongoDB round-trip on retry.

## 2. Module Structure & Responsibilities

```
src/
├── main.py              # App factory, lifespan (init/shutdown clients, create indexes, start worker)
├── config.py            # pydantic-settings BaseSettings, all config from env vars
│
├── events/
│   ├── router.py        # POST /events (202), GET /events
│   ├── schemas.py       # EventCreate, EventAccepted, EventResponse, EventFilter
│   ├── service.py       # Validation, enqueue event, query MongoDB
│   └── dependencies.py  # Route-level deps (pagination, filters)
│
├── analytics/
│   ├── router.py        # GET /events/stats, GET /events/stats/realtime
│   ├── schemas.py       # StatsQuery, RealtimeStatsQuery, StatsResponse, TimeBucket, StatsWindow
│   └── service.py       # Aggregation pipeline builder, realtime stats logic
│
├── search/
│   ├── router.py        # GET /events/search
│   ├── schemas.py       # SearchQuery, SearchResponse
│   └── service.py       # Build ES queries, call AsyncElasticsearch
│
├── queue/
│   ├── base.py          # Queue interface/protocol
│   ├── memory.py        # asyncio.Queue implementation
│   └── dlq.py           # Dead letter queue (bounded)
│
├── ingestion/
│   ├── worker.py        # Consumes batches from queue, bulk writes to MongoDB + ES
│   └── retry.py         # Batch-cycle backoff strategy, retry count tracking
│
├── health/
│   └── router.py        # GET /health — dependency connectivity + pipeline liveness
│
├── cache/
│   ├── service.py       # Redis get/set with TTL, serialization (orjson)
│   └── middleware.py     # Rate limiting middleware (custom, Redis-backed)
│
├── core/
│   ├── database.py      # MongoDB + ES + Redis connection management, index creation
│   ├── logging.py       # structlog configuration
│   └── exceptions.py    # Custom exceptions, exception handlers
│
└── tests/
    ├── unit/
    │   ├── test_schemas.py
    │   ├── test_queue.py
    │   ├── test_worker.py
    │   ├── test_analytics_service.py
    │   └── test_rate_limiter.py
    ├── integration/
    │   ├── conftest.py
    │   ├── test_ingestion.py
    │   ├── test_search.py
    │   ├── test_stats.py
    │   ├── test_realtime_cache.py
    │   └── test_health.py
    └── conftest.py
```

- Route handlers are thin — validate input, call service, return response.
- Service layer contains business logic and database interaction.
- `core/database.py` owns all client lifecycle (init, health checks, shutdown) and index creation for both MongoDB and ES.
- `queue/` owns the transport abstraction (including batch drain logic). `ingestion/` owns the processing logic. Swapping to SQS means adding `queue/sqs.py` — the worker doesn't change.
- `health/` is a thin router that calls `core/database.py` connectivity checks and reads pipeline state (queue depth, DLQ depth, worker liveness).

## 3. Event Schema & Storage

### MongoDB Document

```python
{
    "_id": ObjectId,
    "idempotency_key": "client-uuid-123", # unique index
    "event_type": "pageview",              # indexed
    "timestamp": datetime,                  # indexed
    "user_id": "user_123",                 # indexed
    "source_url": "https://example.com",
    "metadata": { ... },                    # flexible JSON
    "created_at": datetime                  # when ingested
}
```

Standard collections are used rather than MongoDB 8.0 time-series collections. Time-series collections offer ~16x storage compression and 2-3x throughput for append-mostly workloads, but impose restrictions on updates/deletes and have different aggregation behavior. Standard collections provide simpler aggregation pipeline semantics and unrestricted document operations. Time-series collections are a production consideration documented in ARCHITECTURE.md, particularly for high-volume deployments where storage compression becomes material.

### MongoDB Indexes

| Index | Fields | Rationale |
|-------|--------|-----------|
| Unique | `idempotency_key` | Event deduplication — database-enforced uniqueness |
| Compound | `(event_type, timestamp)` | Stats aggregation groups by type and time bucket; compound index covers both the `$match` and `$group` stages |
| Single | `user_id` | GET /events filter by user |
| Single | `source_url` | GET /events filter by source |
| Single | `timestamp` | Date range queries across all event types |

No index on `metadata` — arbitrary structure, and metadata queries are routed to Elasticsearch.

MongoDB indexes are created at startup via `create_index()` (idempotent — safe to call on every startup).

### Elasticsearch Index & Mapping

The ES index is created at startup with an explicit mapping via `indices.create(ignore=400)` (idempotent — 400 means the index already exists, which is ignored). This runs alongside MongoDB index creation in the lifespan startup sequence. Creating the index explicitly at startup is necessary because if the worker's first bulk write hits ES before the index exists, ES auto-creates it with dynamic mapping — the explicit mapping below would be silently overridden, and dynamic mapping on the metadata field specifically causes mapping explosion (individual field mappings per key instead of a single flattened field).

| Field | ES Type | Rationale |
|-------|---------|-----------|
| `_id` | (set to `idempotency_key`) | Idempotent writes — retries overwrite, not duplicate. Cross-store correlation with MongoDB. |
| `event_type` | `keyword` | Exact match filtering |
| `timestamp` | `date` | Range queries |
| `user_id` | `keyword` | Exact match filtering |
| `source_url` | `keyword` | Exact match filtering |
| `metadata` | `flattened` | Keyword-level queries on arbitrary metadata keys/values |
| `metadata_text` | `text` (standard analyzer) | Full-text search across metadata values |

The `flattened` type indexes all metadata values as keywords — supports `term`, `prefix`, `range`, `exists` queries but not full-text analysis. A separate `metadata_text` field with the `standard` analyzer enables full-text search, as required by the assignment ("full-text search across event metadata"). The two fields have complementary, non-overlapping roles: `metadata` (flattened) for structured "I know the key" queries, `metadata_text` for unstructured "search for a value somewhere in the metadata" queries.

The `metadata_text` value is produced by recursive leaf-value extraction: walk the metadata dict, collect all leaf values (strings; numbers converted to string), concatenate with spaces. Keys are excluded — they're queryable via the `flattened` field's `term` queries. For `{"browser": {"name": "Chrome", "version": 120}, "tags": ["promo", "mobile"]}`, the `metadata_text` value is `"Chrome 120 promo mobile"`. This keeps the full-text index focused on values (what users search for) and avoids indexing structural noise (key names, punctuation) that would pollute search results.

A single ES index is used for this scope. Production would use time-based index rotation (data streams with ILM) for performance at scale and retention management. This is documented in ARCHITECTURE.md alongside the general data retention strategy.

### Pydantic API Schemas

- `EventCreate` — input validation: required `idempotency_key`, `event_type`, `timestamp`, `user_id`, `source_url`; optional `metadata` dict. Metadata is validated with defensive limits: maximum serialized size (e.g., 64KB) and maximum nesting depth (e.g., 10 levels). These prevent pathological inputs from causing disproportionate downstream effects (ES bulk write rejections, index bloat, expensive leaf-value extraction) without restricting legitimate use. Validation at the API boundary fails fast with a clear 422 before the event enters the async pipeline.
- `EventAccepted` — POST response (202): echoes the validated input fields including `idempotency_key`. No `id` or `created_at` — the event is accepted for processing but not yet persisted. Producers use this to confirm receipt and for their own bookkeeping (e.g., not re-sending the same idempotency key).
- `EventResponse` — GET response: adds `id` and `created_at` (populated from MongoDB document)
- `EventFilter` — query params: optional `event_type`, `start_date`, `end_date`, `user_id`, `source_url`, plus pagination (`skip`, `limit`). Note: `idempotency_key` is deliberately not included as a filter. The system's consumer model is fire-and-forget — producers send events and move on, consumers query by business dimensions. A producer that needs to retry simply re-sends with the same key; the dedup logic handles it. If a use case arises for looking up events by `idempotency_key` (e.g., a producer dashboard showing ingestion status), the unique index already supports it — the change is one optional query parameter and one filter clause.
- `StatsQuery` — full analytics query: `time_bucket` enum (hourly/daily/weekly), optional `event_type` filter, date range
- `RealtimeStatsQuery` — constrained dashboard query: `window` enum (last 1h, 6h, 24h), optional `event_type` filter. Time bucket is derived from window (e.g., per-minute for 1h, hourly for 24h). No arbitrary date ranges — parameter space is small for high cache hit rates.
- `SearchQuery` — `q` string, optional filters, pagination

## 4. Queue Design & Worker Lifecycle

### Queue Interface (Protocol)

```python
class EventQueue(Protocol):
    async def enqueue(self, message: QueueMessage) -> None: ...
    async def drain(self, max_items: int, timeout: float) -> list[QueueMessage]: ...
    async def ack(self, message_id: str) -> None: ...
    async def nack(self, message_id: str) -> None: ...
```

`QueueMessage` wraps the event payload with metadata: `message_id` (UUID), `retry_count`, `enqueued_at`, `last_error`.

The `drain` method replaces single-item `dequeue`. It returns up to `max_items` messages, blocking up to `timeout` seconds for the first item, then grabbing additional items non-blocking until `max_items` is reached or the queue is empty. This maps directly to SQS `receive_messages` (`MaxNumberOfMessages` + `WaitTimeSeconds`), making the swap mechanical. The batch assembly logic is a transport concern — `asyncio.Queue` and SQS have fundamentally different optimal strategies for it, so it belongs behind the protocol, not in the worker.

### In-Memory Implementation

- Bounded `asyncio.Queue(maxsize=N)` provides backpressure. When full, POST returns 503.
- No visibility timeout or at-least-once guarantees — documented as the key difference from SQS.
- `drain` uses `asyncio.wait_for(queue.get(), timeout)` for the first item (blocks until something arrives or timeout), then `queue.get_nowait()` in a loop for subsequent items (non-blocking, stops when empty or `max_items` reached).
- `ack` calls `task_done()`. `nack` re-enqueues with incremented retry count using non-blocking `put_nowait()`. If the queue is full (`QueueFull`), the item is routed to the DLQ instead — under high load, new POSTs can fill the queue during batch processing, and a blocking put would deadlock (the worker is the only consumer). The DLQ fallback is consistent with the existing overflow pattern. The degradation chain is: nack fails to re-enqueue -> DLQ; DLQ full -> log and drop. SQS eliminates this contention entirely — `nack` maps to `change_message_visibility(0)` on a queue with independent capacity, no competition with new enqueues.

### In-Memory Limitation: No Per-Item Retry Backoff

Per-item retryable ES failures (429/5xx) cause items to be nacked and retried in the next batch cycle with no delay. The retry path is fast: MongoDB dedup succeeds instantly (cheap unique-index lookup), ES upsert via `_id` is safe, so nacked items hit ES again almost immediately. Under sustained ES pressure (e.g., thread pool saturation returning per-item 429s), items cycle through the queue rapidly, burn through their retry budget (default 5), and land in the DLQ — when a slightly longer wait might have succeeded. The source of truth (MongoDB) is intact in this scenario; the impact is items missing from the search index.

The batch-cycle exponential backoff (step 7 in worker lifecycle) only triggers on batch-level connection errors affecting the entire batch — it does not respond to per-item failure rates within an otherwise successful batch.

This is a known limitation of the in-memory implementation. SQS visibility timeout handles retry timing natively: `nack` maps to `change_message_visibility(0)`, and SQS controls redelivery timing independent of the worker's processing speed. The in-memory queue has no equivalent mechanism, and adding one (e.g., failure-rate thresholds triggering batch-level backoff) would introduce a concept with no SQS analogue that complicates the worker for a scenario specific to the transitional implementation.

### Worker Lifecycle

1. Started as `asyncio.Task` in lifespan startup.
2. Loop: call `queue.drain(batch_size, batch_timeout)` to get a batch.
3. Bulk write batch to MongoDB (`insert_many`, `ordered=False`).
4. Handle `BulkWriteError`: build set of failed indexes from `details['writeErrors']`. For each failed item: error code 11000 (`DuplicateKeyError`) = treat as success (already persisted); other errors = nack or route to DLQ based on retry count. Items whose indexes are not in `writeErrors` succeeded.
5. Bulk index succeeded items (including deduplicated) to ES (`async_bulk`), with each document's `_id` set to `idempotency_key`. Items that failed MongoDB with non-duplicate errors are excluded from the ES write.
6. Handle ES per-item results by status code: success = ack. Retryable failure (429, 5xx) = nack (item re-enters queue; on next attempt, MongoDB dedup handles the re-insertion, ES gets another try via idempotent upsert on `_id`). Permanent failure (4xx except 429, e.g., mapping conflict) = route to DLQ immediately, no retry — the event is persisted in MongoDB (source of truth intact), and retrying won't fix the ES rejection. Note: if nack fails due to a full queue, the item is routed to the DLQ (see In-Memory Implementation). Note: per-item retryable failures do not trigger batch-level backoff — see "In-Memory Limitation: No Per-Item Retry Backoff" above.
7. On batch-level retryable failure (connection error, timeout affecting the entire batch): nack all items. Increment a consecutive-batch-failure counter and sleep before the next `drain` using exponential backoff (base 2s, max 60s, jitter) based on that counter. On successful batch, reset the counter. This prevents the worker from spinning under sustained downstream failures. Per-message `retry_count` drives DLQ routing (max retries exceeded, default 5), not timing. Note: SQS handles redelivery timing natively via visibility timeout, so the per-batch sleep is an in-memory-only concern that gets discarded on the SQS swap.
8. Max retries exceeded (default 5): move to DLQ, ack original.
9. Permanent failure (validation error): move to DLQ immediately, no retry.
10. Shutdown: sentinel value enqueued via lifespan shutdown hook. The worker continues its normal drain-process loop until a drain result contains the sentinel. Items from that final drain that preceded the sentinel are processed as a normal batch. Then the worker exits. This guarantees no events are lost during graceful shutdown — uvicorn completes in-flight requests before triggering lifespan shutdown, so all enqueued events precede the sentinel in FIFO order.

### Batch Configuration

- `batch_size`: maximum events per batch (default configurable via `config.py`)
- `batch_timeout`: maximum seconds to wait after first item before flushing (default configurable via `config.py`)
- Under high load, batches fill to `batch_size` quickly — maximizing bulk write throughput.
- Under low load, `batch_timeout` ensures events aren't delayed indefinitely — degrades gracefully to single-event processing.

### Dead Letter Queue

- Second `asyncio.Queue`, **bounded** (`maxsize=M`).
- Messages carry full error history.
- When DLQ is full, messages are logged and dropped — the event's MongoDB write may have already succeeded (source of truth intact), and the failure is recorded in structured logs.
- No automatic retry — in production, a separate process would inspect/replay.

### SQS Comparison (for ARCHITECTURE.md)

- SQS provides visibility timeout, at-least-once delivery, and automatic redelivery. The in-process queue provides none of these.
- SQS survives process crashes. The in-process queue doesn't — in-flight and queued messages are lost on crash.
- SQS handles redelivery timing natively via visibility timeout — the per-batch-cycle backoff sleep in the in-memory implementation is not needed with SQS. This also eliminates the per-item retry backoff limitation: SQS controls redelivery timing for each message independently, so items that failed with 429/5xx are redelivered after visibility timeout rather than immediately.
- SQS eliminates nack-vs-enqueue contention — `change_message_visibility(0)` operates on the queue's own capacity, independent of new message production. The in-memory queue's DLQ fallback on `QueueFull` during nack is not needed with SQS.
- To swap: implement the `EventQueue` protocol with boto3 async — `drain` maps to `receive_messages(MaxNumberOfMessages, WaitTimeSeconds)`, `ack` maps to `delete_message`, `nack` maps to `change_message_visibility(0)`. Add visibility timeout handling, ensure worker idempotency.

## 5. Health Endpoint

`GET /health` — checks dependency connectivity and async pipeline liveness.

### Dependency Checks

- Returns overall status (`healthy`, `degraded`, `unhealthy`) and per-dependency status.
- `healthy`: all three dependencies reachable, worker alive, queue and DLQ below pressure thresholds.
- `degraded`: ES or Redis unreachable (search, cache, or rate limiting unavailable, but core ingestion and analytics functionality works). Also: queue near capacity or DLQ non-empty (pipeline under pressure but still functioning).
- `unhealthy`: MongoDB unreachable — the only dependency whose loss prevents the system's primary function. Also: worker dead — events are accepted but never processed.
- Used as the app's docker-compose health check.

### Pipeline Liveness

The health endpoint includes async pipeline state alongside dependency connectivity:

- **Queue depth**: `queue.qsize()` — indicates backpressure. Near-capacity queue means POSTs are close to returning 503.
- **DLQ depth**: `dlq.qsize()` — indicates processing failures. Non-empty DLQ means events failed permanently or exhausted retries.
- **Worker alive**: boolean flag set by the worker on each loop iteration (e.g., a timestamp of the last successful drain, checked against a staleness threshold). A dead worker means events are accepted but never processed — functionally equivalent to MongoDB being down, hence `unhealthy`.

These are simple reads from objects already in scope — no metrics infrastructure required.

### Production Considerations

In a production Kubernetes deployment, this would be split into separate liveness (cheap, no dependency checks — just return 200) and readiness (dependency connectivity — check MongoDB; ES/Redis failures handled at the application level with per-endpoint 503s) probes. A single combined endpoint is sufficient for this scope. The `degraded`/`unhealthy` classification aligns with the Kubernetes model: `unhealthy` (MongoDB or worker dead) would fail readiness (pod removed from rotation), while `degraded` (ES/Redis down, queue pressure, DLQ non-empty) would still pass readiness (pod continues serving traffic).

## 6. Caching & Rate Limiting

### Redis Caching (Realtime Stats)

- Cache key: `stats:realtime:{hash of query params}`
- Serialization: `orjson`
- TTL: 30 seconds default, configurable via `config.py`
- On cache hit: return immediately.
- On cache miss: run MongoDB aggregation, cache result, return.
- On Redis down: log warning, fall through to MongoDB. Document thundering herd risk and circuit breaker mitigation in ARCHITECTURE.md.
- No active invalidation — TTL-based expiry only. The realtime endpoint is approximate by nature; stale data within the TTL window is acceptable.
- The `RealtimeStatsQuery` schema constrains the parameter space (window enum + optional event_type filter) to ensure high cache hit rates. With ~3 window values and a modest number of event types, the total key space is small — dashboard clients polling the same view get consistent cache hits within the TTL window.

### Rate Limiting Middleware (Custom)

- Redis-backed sliding window counter. This is a demonstration of ASGI middleware design and Redis atomic operations; it is not a production rate limiting solution. In containerized environments, all requests arrive from the same source IP (load balancer, ingress controller), making IP-based application-level rate limiting ineffective. Production rate limiting belongs at the infrastructure level (API gateway, WAF, ingress controller), where real client IPs are visible. This is documented in section 9 (ARCHITECTURE.md) alongside other production evolution notes.
- Key: `ratelimit:{client_ip}:{window}`
- Default: 100 requests per minute per IP, configurable via `config.py`.
- Returns `429 Too Many Requests` with `Retry-After` header.
- On Redis down: fail open (allow request). Rate limiting is best-effort protection, not a security boundary.
- Implemented as ASGI middleware, applies globally before routing. The `/health` endpoint is excluded — health checks must not be subject to rate limiting, as false 429 responses would cause the orchestrator to report the container as unhealthy.

### Event Deduplication

- Client provides a required `idempotency_key` on every POST.
- MongoDB unique index on `idempotency_key` enforces uniqueness at the database level.
- ES documents use `idempotency_key` as their `_id`, making ES writes idempotent — retries overwrite rather than duplicate.
- Worker handles `DuplicateKeyError` (code 11000 in `BulkWriteError.details['writeErrors']`) as a successful outcome — the event was already persisted. The item proceeds to the ES bulk write (in case the prior attempt's ES write failed), where the `_id` ensures idempotent indexing.
- No Redis dependency in the write path, no TTL window, no race conditions.
- Aligns with SQS FIFO `MessageDeduplicationId` pattern — same concept, same field.

## 7. Testing Strategy

### Unit Tests (mocked dependencies)

- `test_schemas.py` — Pydantic validation: required fields, type coercion, invalid input rejected. Covers both `EventCreate`/`EventAccepted` and the constrained `RealtimeStatsQuery`. Includes metadata validation: oversized payloads and deeply nested structures rejected with 422.
- `test_queue.py` — Queue protocol: enqueue/drain ordering, drain respects max_items and timeout, nack re-enqueues with incremented retry count, nack routes to DLQ when queue is full (`QueueFull` fallback), max retries routes to DLQ, sentinel triggers shutdown after processing preceding items.
- `test_worker.py` — Batch processing: batch fills to size, batch flushes on timeout, `BulkWriteError` handling (per-item classification by error code, only succeeded items forwarded to ES), ES per-item failure classification (retryable 429/5xx triggers nack, permanent 4xx triggers DLQ), ES documents use `idempotency_key` as `_id`, batch-cycle backoff (consecutive failure counter increments/resets), DLQ routing, DLQ overflow drops with logging.
- `test_analytics_service.py` — Aggregation pipeline builder: correct `$dateTrunc` unit for each time bucket, filters applied as `$match` stages. Realtime stats window-to-bucket derivation.
- `test_rate_limiter.py` — Counter logic: increments, window expiry, 429 threshold, fail-open on Redis error, health endpoint excluded.

### Integration Tests (testcontainers for MongoDB/ES, fakeredis for Redis)

- `test_ingestion.py` — POST event returns 202 with `EventAccepted` → worker processes batch → GET /events returns it with full `EventResponse`.
- `test_search.py` — POST event → worker processes batch → GET /events/search finds it via ES. Verify `metadata_text` leaf-value extraction enables full-text search on metadata values.
- `test_stats.py` — Ingest multiple events → GET /events/stats returns correct aggregation buckets.
- `test_realtime_cache.py` — GET /events/stats/realtime populates cache → second call served from Redis. Verify constrained query parameters produce consistent cache hits.
- `test_health.py` — Health endpoint returns correct status for healthy, degraded (ES down, Redis down, queue near capacity, DLQ non-empty), and unhealthy (MongoDB down, worker dead) states.

### Test Infrastructure

- Session-scoped testcontainers for MongoDB and ES (expensive to start, reused across tests).
- Function-scoped fakeredis (cheap, clean state per test).
- App factory wires test clients into dependency injection.
- `LifespanManager` from `asgi-lifespan` to trigger startup/shutdown.
- `asyncio_mode = "auto"` in `pyproject.toml`.
- Markers to separate unit vs integration (`pytest -m unit` vs `pytest -m integration`).

### Testing Philosophy & Priorities

**Philosophy:** Test real behavior over mocked contracts. MongoDB and Elasticsearch have complex query semantics (aggregation pipelines, bulk write error structures, full-text analysis) that mocks cannot faithfully reproduce — mongomock has incomplete API coverage and no async support compatible with PyMongo Async, and there is no reliable ES mock. Testcontainers provide real instances at the cost of startup time, mitigated by session-scoped containers reused across tests. Redis is the exception: fakeredis faithfully implements the command surface used here (GET, SET, INCR, EXPIRE), and its async implementation is actively maintained — the speed advantage over a container is worth it for the limited command set.

The unit/integration split optimizes for feedback speed. Unit tests (mocked dependencies, sub-second) cover logic that doesn't depend on database behavior: schema validation, queue protocol mechanics, worker control flow, pipeline construction, rate limiter arithmetic. Integration tests (testcontainers, slower) cover end-to-end request lifecycles where the database behavior is the thing being tested: does the aggregation pipeline return correct buckets, does the ES mapping support the intended queries, does the dual-write retry path actually work.

**With more time:**
- **Load/performance testing**: profile batch throughput under sustained load to validate batch size defaults and identify the single-worker throughput ceiling referenced in the scaling section.
- **Chaos testing of failure paths**: inject MongoDB/ES/Redis failures mid-batch to verify the worker's error classification, backoff, and DLQ routing under realistic failure conditions rather than mocked exceptions.
- **Contract testing for ES mapping**: verify that the ES mapping accepts the full range of metadata structures the API allows, catching mapping conflicts (the permanent 4xx failure path) before they reach production.
- **Fuzz testing of metadata validation**: generate arbitrary nested structures to verify the depth/size validators reject pathological inputs and that the leaf-value extraction handles edge cases (empty dicts, deeply nested arrays, mixed types).

## 8. Docker Setup

### Dockerfile (Application)

- Multi-stage build: builder stage installs deps with `uv`, runtime stage copies the venv.
- Base: `python:3.13-slim`.
- Non-root user.
- Exec-form `CMD` for proper signal handling.

### docker-compose.yml

| Service | Image | Port | Health Check |
|---------|-------|------|-------------|
| app | Built from Dockerfile | 8000 | `curl -f http://localhost:8000/health` |
| mongodb | mongo:8 | 27017 | `mongosh --eval "db.runCommand('ping')"` |
| elasticsearch | elasticsearch:9.1.0 | 9200 | `curl -f http://localhost:9200/_cluster/health` |
| redis | redis:7 | 6379 | `redis-cli ping` |

- ES 9.1.0+ required (elasticsearch-py 9.2+ sends `exclude_vectors`).
- Single-node ES with security disabled for local dev.
- Health checks gate app startup via `depends_on: condition: service_healthy`.
- Named volumes for data persistence across restarts.

## 9. ARCHITECTURE.md Plan

The ARCHITECTURE.md is a first-class deliverable. Structure and content mapped below.

### Section 1: System Diagram

Text/ASCII diagram from section 1 of this design, expanded with batch processing flow, per-item result handling (including ES status code classification), ES `_id` strategy, pipeline health checks, and health endpoint.

### Section 2: Component Responsibilities

What each layer owns and why:
- **API layer** (FastAPI routers): input validation, response serialization, HTTP concerns only. POST returns 202 with validated input; GET endpoints return full documents.
- **Queue** (`asyncio.Queue`): backpressure, decouples ingestion from processing. Transport abstraction (including batch drain) enables SQS swap.
- **Worker**: batch consumption, bulk writes, `BulkWriteError` handling, ES status code classification, batch-cycle backoff, retry/DLQ logic. Owns the write path to both stores. Sets `idempotency_key` as ES document `_id` for idempotent writes.
- **MongoDB**: source of truth for event data, aggregation for analytics.
- **Elasticsearch**: search index for full-text and metadata queries. Derived from MongoDB, not authoritative. Documents keyed by `idempotency_key` for cross-store correlation.
- **Redis**: cache for realtime stats, backing store for rate limiting demonstration. Ephemeral — loss is degradation, not failure.

### Section 3: Storage Rationale

Why responsibility is split the way it is:
- MongoDB for structured queries, aggregation pipelines, and durable storage. Chosen over Elasticsearch as source of truth because of stronger consistency guarantees and transactional support.
- Elasticsearch for full-text search and flexible metadata queries that MongoDB can't efficiently serve (arbitrary nested metadata). Two metadata representations: `flattened` for structured key-based queries, `metadata_text` (leaf-value extraction) for full-text search — complementary, non-overlapping roles.
- Redis for sub-second cache reads and atomic rate-limit counters. Not a durable store.
- Standard collections chosen over MongoDB time-series collections: simpler aggregation semantics, no update/delete restrictions. Time-series collections are a consideration for production deployments where storage compression becomes material.

### Section 4: Failure Modes

| Failure | Impact | Degradation |
|---------|--------|-------------|
| MongoDB down | Writes fail, worker backs off (per-batch-cycle exponential backoff) and retries. Reads on GET /events and GET /events/stats return 503. | Core functionality unavailable. Health endpoint reports `unhealthy`. |
| Elasticsearch down | ES portion of dual write fails with 5xx; MongoDB writes succeed. Items nacked and retried — on retry, MongoDB dedup succeeds (cheap index lookup), ES gets another attempt (idempotent via `_id`). If ES stays down, items exhaust retries and route to DLQ. Search endpoint returns 503. | Search unavailable, all other endpoints functional. Health endpoint reports `degraded`. |
| ES mapping conflict | ES rejects specific items with 4xx. Items routed to DLQ immediately (permanent failure, no retry). MongoDB writes already succeeded — events are persisted, just not searchable. | Affected events missing from search. DLQ surfaces the failures for investigation. |
| Sustained ES pressure (per-item 429s) | Per-item retryable failures nacked and retried without delay (no per-item backoff in in-memory implementation). Items may exhaust retry budget prematurely under sustained pressure. MongoDB writes succeed — events persisted but potentially missing from search index. | Items route to DLQ when a longer wait might have succeeded. SQS visibility timeout eliminates this — redelivery timing is independent of worker speed. |
| Redis down | Cache misses fall through to MongoDB. Rate limiting fails open. | Slightly higher MongoDB load, no rate limiting. Health endpoint reports `degraded`. |
| Worker crash (process dies) | In-flight batch and queued messages lost. | Events accepted via POST are lost. Documented trade-off of in-process queue — SQS would survive this. |
| Worker stall (deadlock, unhandled exception in loop) | Events accepted but never processed. Queue fills, eventually POSTs return 503. | Health endpoint reports `unhealthy` (worker-alive flag stale). |
| Queue full (backpressure) | POST returns 503. | Clients should retry with backoff. No data loss for events not yet accepted. Health endpoint reports `degraded`. |
| Queue full + nack contention | Nack fails to re-enqueue (queue filled by new POSTs during batch processing). Item routed to DLQ instead of retried. | Retry opportunity lost for affected items, but source of truth (MongoDB) likely already has the event. SQS eliminates this contention — nack operates on independent queue capacity. |
| DLQ full | Failed messages logged and dropped. | Source of truth (MongoDB) may already have the event. Failure details in structured logs for investigation. Health endpoint reports `degraded` (DLQ non-empty). |

Thundering herd risk when Redis fails: all realtime stats requests hit MongoDB simultaneously. Production mitigation: circuit breaker pattern — after N Redis failures in a window, short-circuit to MongoDB for a cooldown period rather than attempting Redis on every request.

### Section 5: Scaling Considerations

What breaks at 10x event volume and how to address it:
- **Single worker**: throughput ceiling. Address with multiple worker tasks consuming from the same queue, or move to SQS with horizontal scaling.
- **Batch size tuning**: larger batches amortize bulk write overhead but increase per-batch latency. Needs profiling under load.
- **In-process queue**: memory-bound, lost on crash. Move to SQS for durability and horizontal scaling.
- **Single ES index**: growing index degrades search performance. Move to data streams with ILM for time-based rotation and retention.
- **MongoDB**: single-node bottleneck. Shard by `event_type` or `timestamp` range. Compound index on `(event_type, timestamp)` supports the most expensive query pattern.
- **Redis**: single-node cache. Redis Cluster or read replicas for cache scalability. Rate limiting counters may need distributed coordination.
- **Offset-based pagination**: `skip`/`limit` (MongoDB) and `from`/`size` (ES) degrade at depth — MongoDB scans and discards skipped documents, ES has a default 10,000-hit limit on `from`/`size`. Production would use keyset/cursor-based pagination: last document's `_id` or `(timestamp, _id)` as a cursor for MongoDB, `search_after` for ES. Offset pagination is a scope simplification — the indexes already support cursor-based queries, so the swap is in the API and query layer, not the storage layer.

### Section 6: Data Retention (Production)

- **MongoDB**: TTL index on `created_at` for automatic document expiry. Configurable retention period.
- **Elasticsearch**: under the current dual-write approach, ILM policies with data streams handle index rotation and deletion. Under the CDC approach (change streams), MongoDB TTL deletes propagate as delete events through the change stream — ES cleanup is automatic, no separate ILM needed.
- **Coordination rule**: ES retention >= MongoDB retention to avoid ghost search results pointing to deleted documents.

### Section 7: What Would Change in Production

- **Queue**: SQS for durability, at-least-once delivery, and horizontal scaling. The `EventQueue` protocol abstraction makes this a swap, not a rewrite — `drain` maps to `receive_messages`, `ack` to `delete_message`, `nack` to `change_message_visibility(0)`. SQS visibility timeout provides native redelivery timing, replacing the per-batch-cycle backoff sleep. SQS also eliminates the nack-vs-enqueue contention on the bounded in-memory queue and the per-item retry backoff limitation (redelivery timing is per-message, independent of worker speed).
- **Data sync**: CDC via MongoDB change streams replaces dual write. Decouples ES indexing from the write path. Eliminates the redundant MongoDB round-trip on ES retry. Simplifies data retention (MongoDB TTL drives both stores).
- **Rate limiting**: infrastructure-level (API gateway, WAF) rather than application-level. IP-based rate limiting is ineffective behind a reverse proxy/load balancer where all traffic shares the source IP. The custom middleware demonstrates the pattern but is not a production solution.
- **Health probes**: split into Kubernetes liveness (no dependency checks) and readiness (check MongoDB; ES/Redis failures handled with per-endpoint 503s) probes.
- **Observability**: metrics (Prometheus), distributed tracing (OpenTelemetry), alerting on DLQ depth and worker lag.
- **Time-series collections**: evaluate for storage compression and throughput gains once event volume justifies the trade-offs (restricted updates/deletes, different aggregation behavior).

## 10. README.md Plan

The README.md is a required deliverable. Structure mapped below.

### Section 1: Setup & Run

- Prerequisites: Docker, Docker Compose.
- `docker-compose up` to start all services (MongoDB, ES, Redis, app).
- Environment variables and their defaults (reference `config.py` BaseSettings).
- How to verify the system is running (`GET /health`).

### Section 2: Endpoint Reference

Each route with method, path, request parameters/body, and example response:
- `POST /events` — request body (`EventCreate`), 202 response (`EventAccepted`).
- `GET /events` — query parameters (`EventFilter`), paginated response.
- `GET /events/stats` — query parameters (`StatsQuery`), aggregation response.
- `GET /events/search` — query parameters (`SearchQuery`), search results.
- `GET /events/stats/realtime` — query parameters (`RealtimeStatsQuery`), cached response.
- `GET /health` — health status response (dependency connectivity + pipeline liveness).

### Section 3: Testing

- How to run tests: `pytest -m unit` (fast, no containers), `pytest -m integration` (requires Docker for testcontainers).
- Prerequisites for integration tests (Docker daemon running).
- Testing philosophy and "with more time" priorities (content from section 7 of this design).

## 11. Key Technology Choices

| Concern | Choice | Rationale |
|---------|--------|-----------|
| MongoDB driver | PyMongo 4.16+ Async (no ODM) | Raw driver exposes aggregation/index proficiency to reviewer |
| ES client | elasticsearch-py 9.3 async | Current stable, DSL merged in |
| Redis client | redis-py 7.4 async | Native async, actively maintained |
| Queue | asyncio.Queue + custom worker | Assignment asks to demonstrate queue design decisions |
| Rate limiting | Custom Redis-backed sliding window | Demonstrates ASGI middleware design and Redis atomic operations; IP-based approach is a demo, not production-viable behind a proxy — infrastructure-level rate limiting in production |
| Logging | structlog | Structured JSON, context vars, production-grade |
| Serialization | orjson | Fast JSON for Redis cache values |
| Config | pydantic-settings | Type-safe, env-based configuration |
| Testing | pytest + testcontainers + fakeredis | Real services where fakes fall short, fakes where they're reliable |
| Docker | uv + python:3.13-slim multi-stage | Fast installs, small image |
