# Distributed Event Processing Platform — Design (v2)

v2: Incorporates review feedback from 2026-03-29-design-review.md. See original in 2026-03-29-event-platform-design.md.

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
                                         Batch: drain up to N items or flush on timeout
                                              │
                                         ┌────┴────┐
                                         ▼         ▼
                                      MongoDB    Elasticsearch
                                   (insert_many)  (async_bulk)
                                    source of     dual write,
                                     truth        retry on failure
                                         │
                                         │  On max retries exceeded
                                         ▼
                                     DLQ (asyncio.Queue, bounded)

GET /health ─────────────▶ Check MongoDB + ES + Redis connectivity
GET /events ─────────────▶ MongoDB (filtered query)
GET /events/stats ───────▶ MongoDB (aggregation pipeline)
GET /events/search ──────▶ Elasticsearch (full-text search)
GET /events/stats/realtime ──▶ Redis cache ──miss──▶ MongoDB aggregation
                                                         │
                                                    cache result
```

- All writes go through the queue — the POST handler never touches MongoDB/ES directly.
- MongoDB is the source of truth; Elasticsearch is a search index.
- The worker batches events for bulk writes to both MongoDB and ES.
- If the ES dual write fails for individual items in a batch, the MongoDB writes for those items still succeed; ES failures are logged and retried per-item.
- Redis caches the realtime stats endpoint with configurable TTL; cache misses fall through to MongoDB.
- If Redis is down, the endpoint degrades to a direct MongoDB query (no 500). The thundering herd risk of this fallback is documented in ARCHITECTURE.md with a circuit breaker as the production mitigation.

### Data Consistency: MongoDB ↔ Elasticsearch

Application-level dual write from the worker. Both bulk writes happen in the same worker iteration. If MongoDB succeeds and ES fails for a given item, the event is persisted (source of truth intact) and the ES failure is logged for investigation. This is acceptable for this scope. In production, CDC via MongoDB change streams would replace the dual write for decoupled, eventually-consistent sync.

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
│   └── dlq.py           # Dead letter queue (bounded)
│
├── ingestion/
│   ├── worker.py        # Consumes batches from queue, bulk writes to MongoDB + ES
│   └── retry.py         # Backoff strategy, retry logic
│
├── health/
│   └── router.py        # GET /health — dependency connectivity checks
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
    │   ├── test_realtime_cache.py
    │   └── test_health.py
    └── conftest.py
```

- Route handlers are thin — validate input, call service, return response.
- Service layer contains business logic and database interaction.
- `core/database.py` owns all client lifecycle (init, health checks, shutdown).
- `queue/` owns the transport abstraction. `ingestion/` owns the processing logic. Swapping to SQS means adding `queue/sqs.py` — the worker doesn't change.
- `health/` is a thin router that calls `core/database.py` connectivity checks.

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

A single ES index is used for this scope. Production would use time-based index rotation (data streams with ILM) for performance at scale and retention management. This is documented in ARCHITECTURE.md alongside the general data retention strategy.

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
2. Loop: drain up to `batch_size` items from queue, or flush on `batch_timeout` since first item — whichever comes first.
3. Bulk write batch to MongoDB (`insert_many`). Bulk index batch to ES (`async_bulk`). Both operations return per-item results.
4. Per-item ack/nack based on individual success/failure from bulk results.
5. Retryable failure (connection error, timeout): nack with exponential backoff (base 2s, max 60s, jitter).
6. Max retries exceeded (default 5): move to DLQ, ack original.
7. Permanent failure (validation error, duplicate key): move to DLQ immediately, no retry. `DuplicateKeyError` is treated as a successful ack (event already processed).
8. Shutdown: sentinel value sent via lifespan shutdown, worker flushes remaining batch then exits.

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
- To swap: implement the `EventQueue` protocol with boto3 async, add visibility timeout handling, ensure worker idempotency.

## 5. Health Endpoint

`GET /health` — checks connectivity to MongoDB, ES, and Redis.

- Returns overall status (`healthy`, `degraded`, `unhealthy`) and per-dependency status.
- `healthy`: all three dependencies reachable.
- `degraded`: Redis unreachable (cache and rate limiting unavailable, but core functionality works).
- `unhealthy`: MongoDB or ES unreachable.
- Used as the app's docker-compose health check.
- In a production Kubernetes deployment, this would be split into separate liveness (cheap, no dependency checks — just return 200) and readiness (dependency connectivity) probes. A single combined endpoint is sufficient for this scope.

## 6. Caching & Rate Limiting

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

## 7. Testing Strategy

### Unit Tests (mocked dependencies)

- `test_schemas.py` — Pydantic validation: required fields, type coercion, invalid input rejected.
- `test_queue.py` — Queue protocol: enqueue/dequeue ordering, nack re-enqueues with incremented retry count, max retries routes to DLQ, sentinel triggers shutdown.
- `test_worker.py` — Batch processing: batch fills to size, batch flushes on timeout, per-item ack/nack from bulk results, retryable vs permanent failures, DLQ routing, DLQ overflow drops with logging.
- `test_analytics_service.py` — Aggregation pipeline builder: correct `$dateTrunc` unit for each time bucket, filters applied as `$match` stages.
- `test_rate_limiter.py` — Counter logic: increments, window expiry, 429 threshold, fail-open on Redis error.

### Integration Tests (testcontainers for MongoDB/ES, fakeredis for Redis)

- `test_ingestion.py` — POST event → worker processes batch → GET /events returns it.
- `test_search.py` — POST event → worker processes batch → GET /events/search finds it via ES.
- `test_stats.py` — Ingest multiple events → GET /events/stats returns correct aggregation buckets.
- `test_realtime_cache.py` — GET /events/stats/realtime populates cache → second call served from Redis.
- `test_health.py` — Health endpoint returns correct status for healthy, degraded (Redis down), and unhealthy (MongoDB down) states.

### Test Infrastructure

- Session-scoped testcontainers for MongoDB and ES (expensive to start, reused across tests).
- Function-scoped fakeredis (cheap, clean state per test).
- App factory wires test clients into dependency injection.
- `LifespanManager` from `asgi-lifespan` to trigger startup/shutdown.
- `asyncio_mode = "auto"` in `pyproject.toml`.
- Markers to separate unit vs integration (`pytest -m unit` vs `pytest -m integration`).

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

Text/ASCII diagram from section 1 of this design, expanded with batch processing flow and health endpoint.

### Section 2: Component Responsibilities

What each layer owns and why:
- **API layer** (FastAPI routers): input validation, response serialization, HTTP concerns only.
- **Queue** (`asyncio.Queue`): backpressure, decouples ingestion from processing. Transport abstraction enables SQS swap.
- **Worker**: batch consumption, bulk writes, retry/DLQ logic. Owns the write path to both stores.
- **MongoDB**: source of truth for event data, aggregation for analytics.
- **Elasticsearch**: search index for full-text and metadata queries. Derived from MongoDB, not authoritative.
- **Redis**: cache for realtime stats, backing store for rate limiting. Ephemeral — loss is degradation, not failure.

### Section 3: Storage Rationale

Why responsibility is split the way it is:
- MongoDB for structured queries, aggregation pipelines, and durable storage. Chosen over Elasticsearch as source of truth because of stronger consistency guarantees and transactional support.
- Elasticsearch for full-text search and flexible metadata queries that MongoDB can't efficiently serve (arbitrary nested metadata).
- Redis for sub-second cache reads and atomic rate-limit counters. Not a durable store.
- Standard collections chosen over MongoDB time-series collections: simpler aggregation semantics, no update/delete restrictions. Time-series collections are a consideration for production deployments where storage compression becomes material.

### Section 4: Failure Modes

| Failure | Impact | Degradation |
|---------|--------|-------------|
| MongoDB down | Writes fail, worker nacks and retries with backoff. Reads on GET /events and GET /events/stats return 503. | Core functionality unavailable. Health endpoint reports `unhealthy`. |
| Elasticsearch down | Dual write fails for ES portion; MongoDB writes succeed. Search endpoint returns 503. | Search unavailable, all other endpoints functional. |
| Redis down | Cache misses fall through to MongoDB. Rate limiting fails open. | Slightly higher MongoDB load, no rate limiting. Health endpoint reports `degraded`. |
| Worker crash (process dies) | In-flight batch and queued messages lost. | Events accepted via POST are lost. Documented trade-off of in-process queue — SQS would survive this. |
| Queue full (backpressure) | POST returns 503. | Clients should retry with backoff. No data loss for events not yet accepted. |
| DLQ full | Failed messages logged and dropped. | Source of truth (MongoDB) may already have the event. Failure details in structured logs for investigation. |

Thundering herd risk when Redis fails: all realtime stats requests hit MongoDB simultaneously. Production mitigation: circuit breaker pattern — after N Redis failures in a window, short-circuit to MongoDB for a cooldown period rather than attempting Redis on every request.

### Section 5: Scaling Considerations

What breaks at 10x event volume and how to address it:
- **Single worker**: throughput ceiling. Address with multiple worker tasks consuming from the same queue, or move to SQS with horizontal scaling.
- **Batch size tuning**: larger batches amortize bulk write overhead but increase per-batch latency. Needs profiling under load.
- **In-process queue**: memory-bound, lost on crash. Move to SQS for durability and horizontal scaling.
- **Single ES index**: growing index degrades search performance. Move to data streams with ILM for time-based rotation and retention.
- **MongoDB**: single-node bottleneck. Shard by `event_type` or `timestamp` range. Compound index on `(event_type, timestamp)` supports the most expensive query pattern.
- **Redis**: single-node cache. Redis Cluster or read replicas for cache scalability. Rate limiting counters may need distributed coordination.

### Section 6: Data Retention (Production)

- **MongoDB**: TTL index on `created_at` for automatic document expiry. Configurable retention period.
- **Elasticsearch**: under the current dual-write approach, ILM policies with data streams handle index rotation and deletion. Under the CDC approach (change streams), MongoDB TTL deletes propagate as delete events through the change stream — ES cleanup is automatic, no separate ILM needed.
- **Coordination rule**: ES retention >= MongoDB retention to avoid ghost search results pointing to deleted documents.

### Section 7: What Would Change in Production

- **Queue**: SQS for durability, at-least-once delivery, and horizontal scaling. The `EventQueue` protocol abstraction makes this a swap, not a rewrite.
- **Data sync**: CDC via MongoDB change streams replaces dual write. Decouples ES indexing from the write path. Simplifies data retention (MongoDB TTL drives both stores).
- **Rate limiting**: infrastructure-level (API gateway, WAF) rather than application-level. The custom middleware is sufficient for demonstration but not a security boundary.
- **Health probes**: split into Kubernetes liveness (no dependency checks) and readiness (dependency connectivity) probes.
- **Observability**: metrics (Prometheus), distributed tracing (OpenTelemetry), alerting on DLQ depth and worker lag.
- **Time-series collections**: evaluate for storage compression and throughput gains once event volume justifies the trade-offs (restricted updates/deletes, different aggregation behavior).

## 10. Key Technology Choices

| Concern | Choice | Rationale |
|---------|--------|-----------|
| MongoDB driver | PyMongo 4.16+ Async (no ODM) | Raw driver exposes aggregation/index proficiency to reviewer |
| ES client | elasticsearch-py 9.3 async | Current stable, DSL merged in |
| Redis client | redis-py 7.4 async | Native async, actively maintained |
| Queue | asyncio.Queue + custom worker | Assignment asks to demonstrate queue design decisions |
| Rate limiting | Custom Redis-backed sliding window | Simple, sufficient for this scope; production would use infrastructure-level rate limiting |
| Logging | structlog | Structured JSON, context vars, production-grade |
| Serialization | orjson | Fast JSON for Redis cache values |
| Config | pydantic-settings | Type-safe, env-based configuration |
| Testing | pytest + testcontainers + fakeredis | Real services where fakes fall short, fakes where they're reliable |
| Docker | uv + python:3.13-slim multi-stage | Fast installs, small image |
