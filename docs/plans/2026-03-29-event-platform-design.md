# Distributed Event Processing Platform — Design

## 1. Data Flow & System Architecture

```
Client
  │
  ▼
POST /events ──▶ Validate (Pydantic) ──▶ asyncio.Queue (bounded)
                                              │
                                              ▼
                                         Worker (background asyncio.Task)
                                              │
                                         ┌────┴────┐
                                         ▼         ▼
                                      MongoDB    Elasticsearch
                                    (source of    (dual write,
                                     truth)       retry on failure)
                                         │
                                         │  On max retries exceeded
                                         ▼
                                     DLQ (asyncio.Queue)

GET /events ──────────▶ MongoDB (filtered query)
GET /events/stats ────▶ MongoDB (aggregation pipeline)
GET /events/search ───▶ Elasticsearch (full-text search)
GET /events/stats/realtime ──▶ Redis cache ──miss──▶ MongoDB aggregation
                                                         │
                                                    cache result
```

- All writes go through the queue — the POST handler never touches MongoDB/ES directly.
- MongoDB is the source of truth; Elasticsearch is a search index.
- If the ES dual write fails, the MongoDB write still succeeds; the ES failure is logged and retried.
- Redis caches the realtime stats endpoint with configurable TTL; cache misses fall through to MongoDB.
- If Redis is down, the endpoint degrades to a direct MongoDB query (no 500). The thundering herd risk of this fallback is documented in ARCHITECTURE.md with a circuit breaker as the production mitigation.

### Data Consistency: MongoDB ↔ Elasticsearch

Application-level dual write from the worker. Both writes happen in the same worker iteration. If MongoDB succeeds and ES fails, the event is persisted (source of truth intact) and the ES failure is logged for investigation. This is acceptable for this scope. In production, CDC via MongoDB change streams would replace the dual write for decoupled, eventually-consistent sync.

## 2. Module Structure & Responsibilities

```
src/
├── main.py              # App factory, lifespan (init/shutdown clients, start worker)
├── config.py            # pydantic-settings BaseSettings, all config from env vars
│
├── events/
│   ├── router.py        # POST /events, GET /events
│   ├── schemas.py       # EventCreate, EventResponse, EventFilter
│   ├── service.py       # Validation, enqueue event, query MongoDB
│   └── dependencies.py  # Route-level deps (pagination, filters)
│
├── analytics/
│   ├── router.py        # GET /events/stats, GET /events/stats/realtime
│   ├── schemas.py       # StatsResponse, TimeBucket enum
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
│   └── dlq.py           # Dead letter queue
│
├── ingestion/
│   ├── worker.py        # Consumes from queue, writes to MongoDB + ES
│   └── retry.py         # Backoff strategy, retry logic
│
├── cache/
│   ├── service.py       # Redis get/set with TTL, serialization (orjson)
│   └── middleware.py     # Rate limiting middleware (custom, Redis-backed)
│
├── core/
│   ├── database.py      # MongoDB + ES + Redis connection management
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
    │   └── test_realtime_cache.py
    └── conftest.py
```

- Route handlers are thin — validate input, call service, return response.
- Service layer contains business logic and database interaction.
- `core/database.py` owns all client lifecycle (init, health checks, shutdown).
- `queue/` owns the transport abstraction. `ingestion/` owns the processing logic. Swapping to SQS means adding `queue/sqs.py` — the worker doesn't change.

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

### MongoDB Indexes

| Index | Fields | Rationale |
|-------|--------|-----------|
| Unique | `idempotency_key` | Event deduplication — database-enforced uniqueness |
| Compound | `(event_type, timestamp)` | Stats aggregation groups by type and time bucket; compound index covers both the `$match` and `$group` stages |
| Single | `user_id` | GET /events filter by user |
| Single | `source_url` | GET /events filter by source |
| Single | `timestamp` | Date range queries across all event types |

No index on `metadata` — arbitrary structure, and metadata queries are routed to Elasticsearch.

### Elasticsearch Mapping

| Field | ES Type | Rationale |
|-------|---------|-----------|
| `event_type` | `keyword` | Exact match filtering |
| `timestamp` | `date` | Range queries |
| `user_id` | `keyword` | Exact match filtering |
| `source_url` | `keyword` | Exact match filtering |
| `metadata` | `flattened` | Keyword-level queries on arbitrary metadata keys/values |
| `metadata_text` | `text` (standard analyzer) | Full-text search across metadata values |

The `flattened` type indexes all metadata values as keywords — supports `term`, `prefix`, `range`, `exists` queries but not full-text analysis. A separate `metadata_text` field (stringified metadata values) with the `standard` analyzer enables full-text search. The trade-off is maintaining two representations; this is handled in the worker's serialization step.

### Pydantic API Schemas

- `EventCreate` — input validation: required `idempotency_key`, `event_type`, `timestamp`, `user_id`, `source_url`; optional `metadata` dict
- `EventResponse` — adds `id` and `created_at`
- `EventFilter` — query params: optional `event_type`, `start_date`, `end_date`, `user_id`, `source_url`, plus pagination (`skip`, `limit`)
- `StatsQuery` — `time_bucket` enum (hourly/daily/weekly), optional `event_type` filter, date range
- `SearchQuery` — `q` string, optional filters, pagination

## 4. Queue Design & Worker Lifecycle

### Queue Interface (Protocol)

```python
class EventQueue(Protocol):
    async def enqueue(self, message: QueueMessage) -> None: ...
    async def dequeue(self) -> QueueMessage: ...
    async def ack(self, message_id: str) -> None: ...
    async def nack(self, message_id: str) -> None: ...
```

`QueueMessage` wraps the event payload with metadata: `message_id` (UUID), `retry_count`, `enqueued_at`, `last_error`.

### In-Memory Implementation

- Bounded `asyncio.Queue(maxsize=N)` provides backpressure. When full, POST returns 503.
- No visibility timeout or at-least-once guarantees — documented as the key difference from SQS.
- `ack` calls `task_done()`. `nack` re-enqueues with incremented retry count.

### Worker Lifecycle

1. Started as `asyncio.Task` in lifespan startup.
2. Loop: dequeue → write MongoDB → write ES → ack.
3. Retryable failure (connection error, timeout): nack with exponential backoff (base 2s, max 60s, jitter).
4. Max retries exceeded (default 5): move to DLQ, ack original.
5. Permanent failure (validation error): move to DLQ immediately, no retry.
6. Shutdown: sentinel value sent via lifespan shutdown, worker drains remaining items then exits.

### Dead Letter Queue

- Second `asyncio.Queue` (unbounded).
- Messages carry full error history.
- No automatic retry — in production, a separate process would inspect/replay.

### SQS Comparison (for ARCHITECTURE.md)

- SQS provides visibility timeout, at-least-once delivery, and automatic redelivery. The in-process queue provides none of these.
- SQS survives process crashes. The in-process queue doesn't — in-flight and queued messages are lost on crash.
- To swap: implement the `EventQueue` protocol with boto3 async, add visibility timeout handling, ensure worker idempotency.

## 5. Caching & Rate Limiting

### Redis Caching (Realtime Stats)

- Cache key: `stats:realtime:{hash of query params}`
- Serialization: `orjson`
- TTL: 30 seconds default, configurable via `config.py`
- On cache hit: return immediately.
- On cache miss: run MongoDB aggregation, cache result, return.
- On Redis down: log warning, fall through to MongoDB. Document thundering herd risk and circuit breaker mitigation in ARCHITECTURE.md.
- No active invalidation — TTL-based expiry only. The realtime endpoint is approximate by nature; stale data within the TTL window is acceptable.

### Rate Limiting Middleware (Custom)

- Redis-backed sliding window counter.
- Key: `ratelimit:{client_ip}:{window}`
- Default: 100 requests per minute per IP, configurable via `config.py`.
- Returns `429 Too Many Requests` with `Retry-After` header.
- On Redis down: fail open (allow request). Rate limiting is best-effort protection, not a security boundary.
- Implemented as ASGI middleware, applies globally before routing.

### Event Deduplication

- Client provides a required `idempotency_key` on every POST.
- MongoDB unique index on `idempotency_key` enforces uniqueness at the database level.
- Worker catches `DuplicateKeyError` and treats it as a successful ack (event already processed).
- No Redis dependency in the write path, no TTL window, no race conditions.
- Aligns with SQS FIFO `MessageDeduplicationId` pattern — same concept, same field.

## 6. Testing Strategy

### Unit Tests (mocked dependencies)

- `test_schemas.py` — Pydantic validation: required fields, type coercion, invalid input rejected.
- `test_queue.py` — Queue protocol: enqueue/dequeue ordering, nack re-enqueues with incremented retry count, max retries routes to DLQ, sentinel triggers shutdown.
- `test_worker.py` — Retry logic: retryable vs permanent failures, backoff timing, DLQ routing.
- `test_analytics_service.py` — Aggregation pipeline builder: correct `$dateTrunc` unit for each time bucket, filters applied as `$match` stages.
- `test_rate_limiter.py` — Counter logic: increments, window expiry, 429 threshold, fail-open on Redis error.

### Integration Tests (testcontainers for MongoDB/ES, fakeredis for Redis)

- `test_ingestion.py` — POST event → worker processes → GET /events returns it.
- `test_search.py` — POST event → worker processes → GET /events/search finds it via ES.
- `test_stats.py` — Ingest multiple events → GET /events/stats returns correct aggregation buckets.
- `test_realtime_cache.py` — GET /events/stats/realtime populates cache → second call served from Redis.

### Test Infrastructure

- Session-scoped testcontainers for MongoDB and ES (expensive to start, reused across tests).
- Function-scoped fakeredis (cheap, clean state per test).
- App factory wires test clients into dependency injection.
- `LifespanManager` from `asgi-lifespan` to trigger startup/shutdown.
- `asyncio_mode = "auto"` in `pyproject.toml`.
- Markers to separate unit vs integration (`pytest -m unit` vs `pytest -m integration`).

## 7. Docker Setup

### Dockerfile (Application)

- Multi-stage build: builder stage installs deps with `uv`, runtime stage copies the venv.
- Base: `python:3.13-slim`.
- Non-root user.
- Exec-form `CMD` for proper signal handling.

### docker-compose.yml

| Service | Image | Port | Health Check |
|---------|-------|------|-------------|
| app | Built from Dockerfile | 8000 | — |
| mongodb | mongo:8 | 27017 | `mongosh --eval "db.runCommand('ping')"` |
| elasticsearch | elasticsearch:9.1.0 | 9200 | `curl -f http://localhost:9200/_cluster/health` |
| redis | redis:7 | 6379 | `redis-cli ping` |

- ES 9.1.0+ required (elasticsearch-py 9.2+ sends `exclude_vectors`).
- Single-node ES with security disabled for local dev.
- Health checks gate app startup via `depends_on: condition: service_healthy`.
- Named volumes for data persistence across restarts.

## 8. Key Technology Choices

| Concern | Choice | Rationale |
|---------|--------|-----------|
| MongoDB driver | PyMongo 4.16+ Async (no ODM) | Raw driver exposes aggregation/index proficiency to reviewer |
| ES client | elasticsearch-py 9.3 async | Current stable, DSL merged in |
| Redis client | redis-py 7.4 async | Native async, actively maintained |
| Queue | asyncio.Queue + custom worker | Assignment asks to demonstrate queue design decisions |
| Logging | structlog | Structured JSON, context vars, production-grade |
| Serialization | orjson | Fast JSON for Redis cache values |
| Config | pydantic-settings | Type-safe, env-based configuration |
| Testing | pytest + testcontainers + fakeredis | Real services where fakes fall short, fakes where they're reliable |
| Docker | uv + python:3.13-slim multi-stage | Fast installs, small image |
