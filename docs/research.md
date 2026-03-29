# Domain Research — Distributed Event Processing Platform

Research conducted 2026-03-29. All versions and status verified via web search.

---

## 1. Python Async MongoDB Driver

**Motor is deprecated.** MongoDB officially deprecated Motor (latest 3.7.1, May 2025). Bug fixes only until May 2026, critical fixes until May 2027. No new features.

**PyMongo now has native async.** PyMongo 4.16.0 (Jan 2026) includes `AsyncMongoClient`, `AsyncDatabase`, `AsyncCollection`, `AsyncCursor`, `AsyncClientSession`, and `AsyncChangeStream`. Uses native asyncio (no thread pool delegation like Motor), resulting in better latency and throughput.

Migration from Motor is straightforward — replace `MotorClient` with `AsyncMongoClient` and update imports.

### ODMs

| ODM | Version | Last Release | Async | Driver | Pydantic v2 | Status |
|-----|---------|-------------|-------|--------|-------------|--------|
| **Beanie** | 2.1.0 | Mar 2026 | Yes | PyMongo Async | Yes | Active, JetBrains-sponsored |
| ODMantic | 1.1.0 | Jan 2025 | Yes | Motor (deprecated) | Yes | Risky — still depends on Motor |
| MongoEngine | 0.29.3 | Mar 2026 | No | PyMongo sync | N/A | Not suitable for async FastAPI |

Beanie 2.0 dropped Motor and moved to PyMongo Async. Beanie 2.1 requires Python >= 3.10 and Pydantic v2 only. Has built-in declarative index definitions, schema migrations, relationship support, and aggregation helpers.

**Alternative: no ODM.** PyMongo Async directly with Pydantic models gives maximum control and fewer abstractions.

### Aggregation Pipelines

- Raw pipeline dicts are the standard approach and map directly to Python dicts
- **monggregate** (0.21.0): OOP/fluent pipeline builder, small project (22 stars)
- **PyMongoArrow** (1.13.0): Official MongoDB lib for MongoDB → Arrow/Pandas/NumPy conversion, useful for analytics

### Index Creation

- `AsyncCollection.create_index()` is idempotent — safe to call at startup
- Beanie: declare indexes on Document class via `Indexed()` type hint and `Settings.indexes`, auto-created at `init_beanie()`

---

## 2. Elasticsearch Python Client

**elasticsearch-py 9.3.0** (Feb 2026). Actively maintained. Requires Python >= 3.10.

**elasticsearch-dsl is deprecated as standalone.** The DSL module (8.18.0, Apr 2025 — final standalone release) is now merged into `elasticsearch` 9.x. Imports change: `from elasticsearch_dsl import ...` → `from elasticsearch.dsl import ...`.

### Breaking Changes in 9.x

- Six deprecated constructor params renamed (e.g., `timeout` → `request_timeout`)
- kNN search API removed (use `knn` param in search API)
- 9.2.0: DSL sends `exclude_vectors` in all search requests, requiring ES server >= 9.1.0

### Async Support

Built into the main package. Install with `pip install elasticsearch[async]` (pulls in aiohttp).

```python
from elasticsearch import AsyncElasticsearch
client = AsyncElasticsearch("https://localhost:9200")
# Must call await client.close() on shutdown
```

Async helpers: `async_bulk`, `async_scan`, `async_streaming_bulk`, `async_reindex`.

### Index Mapping for Event Data

- **Use explicit mappings** — do not rely on dynamic mapping in production
- `keyword` for categorical fields (event type, status, source)
- `date` with explicit format for timestamps
- `text` only for fields needing full-text search
- **`flattened` type** for arbitrary metadata: indexes all values as keywords, supports `term`, `prefix`, `range`, `match`, `exists` queries. Limitation: no numeric range queries, no full-text analysis on values.
- Multi-field pattern for both full-text and exact match:
  ```json
  { "title": { "type": "text", "analyzer": "english", "fields": { "raw": { "type": "keyword" } } } }
  ```
- Mapping explosion risk with dynamic mapping of arbitrary JSON — set `index.mapping.total_fields.limit`
- Time-based index strategy (data streams or index-per-period) with ILM for retention

### Pitfalls

- Never pass raw user-provided search bodies to ES — build queries server-side
- Use `AsyncElasticsearch`, not sync client, in async FastAPI
- Must `await client.close()` on shutdown or connections leak
- Use `async_bulk` for batch writes — single document indexing is slow
- For long operations (`delete_by_query`), use `wait_for_completion=False`

---

## 3. Redis Python Client

**redis-py 7.4.0** (Mar 2026). Requires Python >= 3.10.

**aioredis is dead.** Last release 2.0.1 (Dec 2021), repo under `aio-libs-abandoned`. Async merged into redis-py as `redis.asyncio` since v4.2.0.

### Async Usage

```python
import redis.asyncio as redis
client = redis.Redis(host="localhost", port=6379, decode_responses=True)
await client.ping()
await client.aclose()  # Must call explicitly — no destructor
```

### Connection Management

- Use `ConnectionPool.from_url()` with explicit `max_connections` (default is unbounded)
- **Do NOT use `Redis.from_pool()` in threaded contexts** — closing one client kills the pool for all. Use `Redis(connection_pool=pool)` instead.
- Store client on `app.state` via lifespan context manager

### Caching Libraries for FastAPI

| Library | Version | Last Release | Status |
|---------|---------|-------------|--------|
| **cashews** | 7.5.0 | Mar 2026 | Active; framework-agnostic, FastAPI middleware, multiple strategies |
| **fastapi-cache2-fork** | 2.3.0 | Jan 2026 | Active fork; uses msgspec for serialization |
| fastapi-cache2 | 0.2.2 | Jul 2024 | Possibly stale |

### Serialization

| Serializer | Speed | Size | Safety |
|-----------|-------|------|--------|
| **msgspec** (JSON or MessagePack) | Fastest | Small-Medium | Safe |
| **orjson** | Very fast | Medium | Safe |
| **msgpack/ormsgpack** | Very fast | Smallest (binary) | Safe |
| stdlib json | Slow | Large | Safe |
| pickle | Fast | Medium | **UNSAFE** — arbitrary code execution |

### Pitfalls

- Must call `aclose()` — connections leak silently without it
- Don't create a new Redis client per request — defeats pooling
- Never use `KEYS` in production (blocks Redis) — use `SCAN`
- Set explicit `max_connections` on pool
- Set `decode_responses=True` or all values come back as bytes
- Handle Redis downtime gracefully — cache miss should fall through to DB, not 500

---

## 4. In-Process Async Queue

### asyncio.Queue

- Use **bounded queues** (`asyncio.Queue(maxsize=N)`) for backpressure
- Always call `task_done()` after processing; use `join()` to wait for completion
- Use sentinel values (poison pills) for graceful shutdown
- `asyncio.wait_for(queue.get(), timeout)` is race-condition-free (cpython#92824)

### FastAPI BackgroundTasks vs Custom Workers

- `BackgroundTasks`: fire-and-forget, no status tracking, no retry. Suitable for lightweight side-effects.
- **Custom worker pattern (recommended)**: long-lived `asyncio.Task` created at startup via lifespan, consuming from an `asyncio.Queue`. Gives control over retry logic, concurrency, dead-lettering.
- Graduation path: `BackgroundTasks` → `asyncio.Queue` workers → external broker (ARQ/Taskiq/Celery)

### Retry / Backoff

**tenacity 9.1.4** (Feb 2026): Python >= 3.10, full async support via `AsyncRetrying` and `@retry` decorator on async functions. Supports stop conditions, wait strategies (exponential, random, fixed, chain), retry conditions, callbacks.

Manual implementation is also straightforward for simple cases.

### Dead Letter Queue Simulation

Two `asyncio.Queue` instances: main queue + DLQ. Worker attempts processing, re-enqueues with incremented attempt count on retryable errors, moves to DLQ on permanent errors or max retries exceeded. Job messages carry metadata: `retry_count`, `error_history`, `original_timestamp`, `last_error`.

### SQS-Style At-Least-Once Delivery

`asyncio.Queue` does not natively support visibility timeouts or at-least-once semantics. Requires a custom wrapper:
- "In-flight" dict keyed by message ID with timestamps
- Background task checks for expired in-flight messages and re-enqueues
- Consumers must explicitly ack after processing
- Consumers must be idempotent (messages can be delivered more than once)

No existing library provides this out of the box for in-process use.

### In-Process Queue Libraries

| Library | Version | In-Process | Notes |
|---------|---------|------------|-------|
| **asyncio.Queue** (stdlib) | Python 3.10+ | Yes | Best option for truly in-process |
| Taskiq | 0.12.1 (Dec 2025) | Has `InMemoryBroker` | Designed for distributed use, alpha status |
| SAQ | 0.26.3 (Mar 2026) | No — requires Redis/PG | Very fast but not in-process |
| ARQ | 0.27.0 (Feb 2026) | No — requires Redis | Now maintenance-only |

---

## 5. FastAPI Framework & Project Structure

### FastAPI Current State

**FastAPI 0.135.2** (Mar 2026). Starlette 1.0.0 (reached stable). Pydantic 2.12.5. Uvicorn 0.42.0. Requires Python >= 3.10.

Notable recent changes:
- **0.132.0 BREAKING**: Strict `Content-Type` checking enabled by default — requests without `application/json` header are rejected. Disable with `strict_content_type=False`.
- **0.131.0**: Deprecated `ORJSONResponse` and `UJSONResponse` — Pydantic-based serialization replaces them
- **0.130.0**: Pydantic-based JSON serialization (2x+ performance when return types annotated)
- **0.135.0**: Server-Sent Events (SSE) support

### Recommended Project Layout

Domain-based modules, not layer-based (per [zhanymkanov/fastapi-best-practices](https://github.com/zhanymkanov/fastapi-best-practices), ~6k stars):

```
src/
├── main.py              # App factory, lifespan, mount routers
├── config.py            # Global BaseSettings
├── events/
│   ├── router.py        # APIRouter endpoints
│   ├── schemas.py       # Pydantic request/response models
│   ├── models.py        # DB document models
│   ├── service.py       # Business logic
│   └── dependencies.py  # Module-specific DI
├── analytics/
├── search/
├── core/
│   ├── database.py      # Connection management
│   ├── middleware.py
│   └── exceptions.py
└── tests/
```

### Key Patterns

- **Lifespan context manager** — `@app.on_event` is deprecated. Use `@asynccontextmanager async def lifespan(app):`
- **Dependency injection** — deps cached per-request; use chain dependencies for composed validation
- **pydantic-settings 2.13.1** for config — `BaseSettings` with `SettingsConfigDict(env_file=".env")`
- **Separate DB models from API schemas**
- **Service layer** — keep route handlers thin

### Pydantic v2 Gotchas

- `class Config` → `model_config = ConfigDict(...)`
- `orm_mode` → `from_attributes`
- `@validator` → `@field_validator`, `@root_validator` → `@model_validator`
- `.dict()` → `.model_dump()`, `.json()` → `.model_dump_json()`

### Logging

| Library | Version | Notes |
|---------|---------|-------|
| **structlog** | 25.5.0 | Recommended for production — structured JSON output, context vars, integrates with stdlib |
| loguru | 0.7.3 | Simpler setup but less structured |

structlog with `ProcessorFormatter` routes all logs (yours + uvicorn + starlette) through one pipeline. Use `contextvars.bind_contextvars()` in middleware for request-scoped context (request_id, user_id).

### Rate Limiting

| Library | Version | Last Release | Notes |
|---------|---------|-------------|-------|
| **slowapi** | 0.1.9 | Feb 2024 | Most widely used (1.9k stars), multiple backends, but stale |
| **fastapi-limiter** | 0.2.0 | Feb 2026 | Recently updated, Redis-only, dependency-based |

### Docker

- **uv** is the emerging standard for Python dependency management (10-100x faster than pip)
- Multi-stage builds with `python:3.13-slim` base
- Non-root user for security
- Exec form `CMD` (array syntax) for proper signal handling
- `depends_on` with `condition: service_healthy` in docker-compose

---

## 6. Testing

### pytest-asyncio 1.3.0 (Nov 2025)

- Default `asyncio_mode` is now `strict` — must explicitly set `asyncio_mode = "auto"` in config for auto-detection of async tests
- Supports `loop_scope` parameter for event loop lifetime control

### MongoDB Testing

| Option | Version | Pros | Cons |
|--------|---------|------|------|
| **mongomock** | 4.3.0 (Nov 2024) | Fast, in-memory | Incomplete API, sync only |
| **testcontainers[mongodb]** | 4.14.2 (Mar 2026) | Real MongoDB | Docker overhead |

mongomock-motor (0.0.36) exists but depends on Motor (deprecated). For new projects using PyMongo Async, mongomock may not be directly compatible — testcontainers becomes more important.

### Elasticsearch Testing

| Option | Version | Notes |
|--------|---------|-------|
| **pytest-elasticsearch** | 5.0.0 (Feb 2026) | Manages real ES process, ES >= 8.0 |
| **testcontainers[elasticsearch]** | 4.14.2 | Docker-based |
| ElasticMock | — | Pure Python mock, incomplete API |

### Redis Testing

**fakeredis 2.34.1** (Feb 2026): Actively maintained. `FakeAsyncRedis` works with `redis.asyncio`. Supports RedisJSON, Geo, Lua scripting, probabilistic data structures.

### FastAPI Testing

Use `httpx.AsyncClient` with `ASGITransport`, not the sync `TestClient`:
```python
async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
    response = await client.get("/items/1")
```

**Critical**: `AsyncClient` does NOT trigger lifespan events. Use `asgi-lifespan` (`LifespanManager`) to wrap the app.

### Common Testing Pitfalls

- Event loop scope mismatch between session-scoped fixtures and function-scoped tests
- `asyncio_mode` defaulting to `strict` in pytest-asyncio 1.3.0 — async tests silently not running as async
- Lifespan events not firing with `AsyncClient`
- Objects requiring an event loop instantiated at module level → "Task attached to a different loop"

---

## 7. Prior Art & Architectural Patterns

### Data Consistency: MongoDB ↔ Elasticsearch

Three patterns in order of increasing reliability:

1. **Application-level dual write**: Write to both in same handler. No atomicity — if one fails, data diverges. Acceptable only for prototypes.
2. **Transactional outbox + relay**: Write to MongoDB with outbox document in same transaction; background relay polls and indexes to ES. Guarantees eventual consistency. At-least-once delivery.
3. **Change Data Capture (CDC) via MongoDB change streams**: Listen to change streams, propagate to ES. Decoupled from write path, near real-time, resume tokens for fault tolerance. Requires replica set. **Monstache** (1.3k stars) is a turnkey Go daemon for this; alternatively, use PyMongo `AsyncChangeStream`.

Recommended production pattern: MongoDB as single source of truth, CDC feeding ES. Query ES for search/discovery, fetch full documents from MongoDB by ID.

### Time-Bucketed Aggregation

MongoDB 5.0+ `$dateTrunc` is standard:
```javascript
{ $group: { _id: { $dateTrunc: { date: "$timestamp", unit: "hour" } }, count: { $sum: 1 } } }
```

Performance: `$match` early, compound indexes on `(timestamp, filter_fields)`, consider pre-aggregation into summary collections for high-traffic dashboards.

MongoDB 8.0 time-series collections offer ~16x storage compression and 2-3x throughput with block processing. Restrictions: limited updates/deletes. Best for append-mostly data.

### Backpressure

- Uvicorn has no built-in backpressure by default — under overload, event loop queues all requests until OOM
- Mitigations: `uvicorn --limit-concurrency N`, bounded `asyncio.Queue`, application-level semaphores, external rate-limiting proxy
- Reference: Armin Ronacher's "I'm not feeling the async pressure"

### Reference Projects

- **full-stack-fastapi-mongodb** (MongoDB Labs, 811 stars): cookiecutter template
- **stac-fastapi-elasticsearch-opensearch** (67 stars): well-structured FastAPI + ES integration
- **fastapi-events**: ASGI middleware event dispatching with Pydantic validation
- **Monstache** (1.3k stars): MongoDB → Elasticsearch real-time sync via change streams

---

## Version Summary

| Component | Package | Version | Python |
|-----------|---------|---------|--------|
| Web framework | fastapi | 0.135.2 | >= 3.10 |
| ASGI framework | starlette | 1.0.0 | >= 3.10 |
| Validation | pydantic | 2.12.5 | >= 3.9 |
| Settings | pydantic-settings | 2.13.1 | >= 3.9 |
| ASGI server | uvicorn | 0.42.0 | >= 3.10 |
| MongoDB driver | pymongo (async) | 4.16.0 | >= 3.9 |
| MongoDB ODM | beanie | 2.1.0 | >= 3.10 |
| Elasticsearch | elasticsearch | 9.3.0 | >= 3.10 |
| Redis | redis | 7.4.0 | >= 3.10 |
| Retry | tenacity | 9.1.4 | >= 3.10 |
| Logging | structlog | 25.5.0 | — |
| Rate limiting | fastapi-limiter | 0.2.0 | — |
| Rate limiting | slowapi | 0.1.9 | — |
| Caching lib | cashews | 7.5.0 | — |
| Serialization | orjson | — | — |
| Serialization | msgspec | 0.20.0 | — |
| Test: async | pytest-asyncio | 1.3.0 | >= 3.10 |
| Test: MongoDB | mongomock | 4.3.0 | — |
| Test: Redis | fakeredis | 2.34.1 | — |
| Test: ES | pytest-elasticsearch | 5.0.0 | >= 3.10 |
| Test: containers | testcontainers | 4.14.2 | >= 3.10 |
| Test: HTTP | httpx | — | — |
| Test: lifespan | asgi-lifespan | — | — |
