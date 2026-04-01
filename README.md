# Birdwatcher

Distributed event processing platform. Accepts events via async ingestion pipeline, stores in MongoDB, indexes in Elasticsearch for full-text search, and caches realtime stats in Redis.

## Setup & Run

### Prerequisites

- Python >= 3.13
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (manages Python versions and dependencies)
- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) (v2)

### Install Dependencies

```bash
uv sync --extra dev
```

### Start

```bash
docker compose up --build
```

This starts the application along with MongoDB 8, Elasticsearch 9.1, and Redis 7. The app waits for all dependencies to be healthy before accepting traffic.

Services are exposed on these default ports:

| Service         | Port |
|-----------------|------|
| App (FastAPI)   | 8000 |
| MongoDB         | 27017 |
| Elasticsearch   | 9200 |
| Redis           | 6379 |

### Environment Variables

Copy `.env.example` for reference. All variables are prefixed with `APP_` and have sensible defaults — the Docker Compose file sets the necessary connection URLs automatically.

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_MONGODB_URL` | `mongodb://localhost:27017` | MongoDB connection string |
| `APP_MONGODB_DATABASE` | `birdwatcher` | Database name |
| `APP_ELASTICSEARCH_URL` | `http://localhost:9200` | Elasticsearch connection string |
| `APP_ELASTICSEARCH_INDEX` | `events` | Elasticsearch index name |
| `APP_REDIS_URL` | `redis://localhost:6379` | Redis connection string |
| `APP_REDIS_MAX_CONNECTIONS` | `20` | Redis connection pool size |
| `APP_QUEUE_MAX_SIZE` | `10000` | Bounded in-memory queue capacity |
| `APP_DLQ_MAX_SIZE` | `1000` | Dead letter queue capacity |
| `APP_BATCH_SIZE` | `100` | Worker batch size for bulk writes |
| `APP_BATCH_TIMEOUT` | `5.0` | Seconds before flushing a partial batch |
| `APP_MAX_RETRIES` | `5` | Max retries before routing to DLQ |
| `APP_REALTIME_STATS_TTL` | `30` | Redis cache TTL in seconds for realtime stats |
| `APP_RATE_LIMIT_REQUESTS` | `100` | Requests allowed per window |
| `APP_RATE_LIMIT_WINDOW` | `60` | Rate limit window in seconds |

### Verify

```bash
curl http://localhost:8000/health
```

A healthy response:

```json
{
  "status": "healthy",
  "dependencies": {
    "mongodb": "up",
    "elasticsearch": "up",
    "redis": "up"
  },
  "pipeline": {
    "worker_alive": true,
    "queue_depth": 0,
    "dlq_depth": 0
  }
}
```

---

## Endpoint Reference

### `POST /events`

Validate and enqueue an event for async processing. Returns immediately — the event is written to MongoDB and Elasticsearch by the background worker.

- **Status:** `202 Accepted`
- **Error:** `503` if the queue is full, `422` for validation errors, `429` if rate-limited

**Request body** (`EventCreate`):

```json
{
  "idempotency_key": "abc-123",
  "event_type": "page_view",
  "timestamp": "2026-03-29T12:00:00Z",
  "user_id": "user-42",
  "source_url": "https://example.com/page",
  "metadata": {"browser": "Firefox", "os": "Linux"}
}
```

**Response** (`EventAccepted`):

```json
{
  "idempotency_key": "abc-123",
  "event_type": "page_view",
  "timestamp": "2026-03-29T12:00:00Z",
  "user_id": "user-42",
  "source_url": "https://example.com/page",
  "metadata": {"browser": "Firefox", "os": "Linux"}
}
```

**Validation rules:**
- `metadata` must be under 64 KB serialized and at most 10 levels deep
- `timestamp` must be within 30 days in the past and 5 minutes in the future

---

### `GET /events`

Query persisted events from MongoDB with optional filters and pagination.

**Query parameters:**

| Parameter    | Type     | Default | Description |
|-------------|----------|---------|-------------|
| `event_type` | string  | —       | Filter by event type |
| `user_id`    | string  | —       | Filter by user ID |
| `source_url` | string  | —       | Filter by source URL |
| `start_date` | datetime | —      | Inclusive lower bound on timestamp |
| `end_date`   | datetime | —      | Inclusive upper bound on timestamp |
| `skip`       | int     | `0`     | Number of results to skip (>= 0) |
| `limit`      | int     | `50`    | Results per page (1–200) |

**Example request:**

```
GET /events?event_type=page_view&limit=2
```

**Example response:**

```json
[
  {
    "id": "6608...",
    "idempotency_key": "abc-123",
    "event_type": "page_view",
    "timestamp": "2026-03-29T12:00:00Z",
    "user_id": "user-42",
    "source_url": "https://example.com/page",
    "metadata": {"browser": "Firefox", "os": "Linux"},
    "created_at": "2026-03-29T12:00:01Z"
  }
]
```

---

### `GET /events/stats`

Aggregated event counts grouped by event type and time bucket, via MongoDB aggregation pipeline.

**Query parameters:**

| Parameter    | Type     | Required | Default | Description |
|-------------|----------|----------|---------|-------------|
| `time_bucket` | string | Yes      | —       | `hourly`, `daily`, or `weekly` |
| `event_type`  | string | No       | —       | Filter by event type |
| `start_date`  | datetime | No     | —       | Inclusive lower bound |
| `end_date`    | datetime | No     | —       | Inclusive upper bound |

**Date range limits per bucket:** hourly max 7 days, daily max 365 days, weekly max 730 days.

**Example request:**

```
GET /events/stats?time_bucket=daily&event_type=page_view&start_date=2026-03-01T00:00:00Z&end_date=2026-03-29T00:00:00Z
```

**Example response:**

```json
{
  "buckets": [
    {"time_bucket": "2026-03-01T00:00:00Z", "event_type": "page_view", "count": 142},
    {"time_bucket": "2026-03-02T00:00:00Z", "event_type": "page_view", "count": 89}
  ]
}
```

---

### `GET /events/stats/realtime`

Recent event counts from Redis cache (falls back to MongoDB aggregation on cache miss). Designed for dashboard-style polling with predictable performance.

**Query parameters:**

| Parameter    | Type   | Default | Description |
|-------------|--------|---------|-------------|
| `window`     | string | `1h`   | Time window: `1h`, `6h`, or `24h` |
| `event_type` | string | —      | Filter by event type |

**Example request:**

```
GET /events/stats/realtime?window=1h
```

**Example response:**

```json
{
  "buckets": [
    {"time_bucket": "2026-03-29T11:00:00Z", "event_type": "page_view", "count": 37},
    {"time_bucket": "2026-03-29T11:00:00Z", "event_type": "click", "count": 12}
  ]
}
```

---

### `GET /events/search`

Full-text search across event metadata via Elasticsearch.

**Query parameters:**

| Parameter    | Type     | Required | Default | Description |
|-------------|----------|----------|---------|-------------|
| `q`          | string  | Yes      | —       | Search query (matched against metadata text) |
| `event_type` | string  | No       | —       | Filter by event type |
| `user_id`    | string  | No       | —       | Filter by user ID |
| `start_date` | datetime | No      | —       | Inclusive lower bound on timestamp |
| `end_date`   | datetime | No      | —       | Inclusive upper bound on timestamp |
| `skip`       | int     | No       | `0`     | Number of results to skip (>= 0) |
| `limit`      | int     | No       | `20`    | Results per page (1–100) |

**Example request:**

```
GET /events/search?q=Firefox&event_type=page_view
```

**Example response:**

```json
{
  "hits": [
    {
      "id": "abc-123",
      "event_type": "page_view",
      "timestamp": "2026-03-29T12:00:00Z",
      "user_id": "user-42",
      "source_url": "https://example.com/page",
      "metadata": {"browser": "Firefox", "os": "Linux"},
      "score": 1.23
    }
  ],
  "total": 1
}
```

---

### `GET /health`

Reports system health including dependency connectivity, worker liveness, and queue depths.

**Status logic:**
- `healthy` — all dependencies up, worker alive, queue not near capacity, DLQ empty
- `degraded` — Elasticsearch or Redis down, DLQ has items, or queue above 90% capacity
- `unhealthy` — MongoDB down or worker dead

See [Verify](#verify) above for the response format.

---

## Testing

### Unit Tests

```bash
uv run pytest -m unit
```

Fast, no external dependencies. Uses `fakeredis` for Redis and plain mocks for MongoDB/Elasticsearch. Covers core logic: queue operations, schema validation, service methods, error classification, and middleware behavior.

### Integration Tests

```bash
uv run pytest -m integration
```

Requires Docker. Uses `testcontainers` to spin up real MongoDB and Elasticsearch instances. Tests full request lifecycles through the actual async pipeline: POST → worker processing → GET verification. Synchronizes with the async worker via `queue.join()` for deterministic assertions without polling or sleeps.

### Philosophy

Testing priorities follow the risk profile: unit tests cover the branching logic (error classification, retry/DLQ routing, validation edge cases) where bugs are most likely, while integration tests verify that the end-to-end pipeline actually works with real databases. The goal is confidence in the system's behavior, not coverage metrics.

---

## AI in My Workflow

This project was built with Claude Code, using a combination of custom skills and agents, with implementation execution done via the superpowers plugin.

### How AI Was Used

**Domain Research.** Claude Code was used to do domain research into best practices for similar systems, up-to-date library information, common pitfalls, etc. This was critical, as Claude Code training knowledge was out of date and wanted to use several deprecated libraries (see Where I Pushed Back section below)

**Design iteration.** The system design went through 7 revisions. Each version was drafted collaboratively, then subjected to a structured review pass where Claude analyzed the design for gaps, contradictions, and unaddressed failure modes. This produced dozens of design revisions and issues caught — things like "graceful shutdown deadlocks if the worker is dead" and "no bounds validation on client-provided timestamps" — that were resolved before implementation began. In the future I would have done this in a more structured way to ensure review decisions were properly scoped to the design phase, and was more strict about adhering to initial requirements, as I ended up having to refactor or change several things that were iterated on in this review cycle.

**Implementation planning.** The final design was translated into a sequenced 22-task implementation plan with explicit file lists, step-by-step instructions, and test expectations for each task. Claude generated this plan from the design document, and I reviewed/adjusted the sequencing and scope.

**Code generation with TDD.** Each task followed a test-first pattern: write tests for the expected behavior, then implement until they pass. Claude generated both the tests and implementation code, which I reviewed for correctness against the design document and library documentation.

**After initial implementation.** Claude Code was used to actively seek out issues and gaps, finding a number of bugs as well as implementation misses. Additionally several refactors were done to address mistakes and poor organization/

### Where I Pushed Back

**API version incompatibilities.** Claude's initial designs referenced Motor (the async MongoDB driver), which is deprecated as of 2025. Research revealed that PyMongo 4.16+ now has native async support via `AsyncMongoClient`. Similarly, the Elasticsearch Python client underwent significant API changes in v9 — methods like `helpers.async_bulk` changed their return types and error handling semantics compared to v8 examples that appear in most training data. These required verifying current library documentation rather than relying on AI-generated code patterns. This prompted a dedicated research step before beginning the final design.

**Library API changes.** Several generated code snippets used outdated APIs — for example, Elasticsearch bulk helper patterns from v7/v8 that don't work with v9's `BulkIndexError` exception handling, and `testcontainers` configuration patterns that changed between major versions. Each case required consulting the actual library source or docs to get correct, current usage.

**Project Structure** Claude frequently confused some concepts, grouping code together that were separate modules. Claude leaned towards making modules that were single use or conflated ideas. For instance it made a cache module that had a single small redis helper and our rate-limiting middleware (which didn't belong there).

**Over Engineering** Claude correctly identified several gaps and potential issues that would result in this project not being production ready, particularly around the use of an in-memory queue. Even with the in-memory queue being an explicit part of the project requirements, I frequently had to steer it away from over-engineering aspects that were out of scope.

**Missing types and inconsistent use of type safety** Claude's implementation often skipped type definitions, or mistyped data. This prompted me to add pyright and do a manual pass to check types. This type checking caught a number of type issues, as well as some legitimate bugs.

**Lack of code comments** A number of code iterations, fixes, and improvements lacked appropriate code comments in places where intention and reasoning were not clear. I did a separate pass after implementation with Claude to add code comments to address this.

### How It Shaped the Approach

The research-then-design-then-review cycle was the most valuable pattern. Having an AI reviewer that can systematically scan for failure modes and edge cases — across 7 iterations — caught issues that would have surfaced much later during implementation or testing. The structured review format (issue → decision → alternatives considered → rationale) also forced explicit documentation of design decisions, which made the implementation plan more precise and reduced ambiguity during coding. The research before design caught a number of issues with out of date and deprecated libraries.
