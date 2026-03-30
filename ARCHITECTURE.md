# Architecture

This document describes the system architecture of the Distributed Event Processing Platform: how data flows through the system, what each component is responsible for, why storage responsibilities are split the way they are, how failures are handled, and what would change in production.

## 1. System Diagram

```
Client
  |
  v
POST /events --> Validate (Pydantic) --> asyncio.Queue (bounded)
  |                                            |
  v                                            v
202 Accepted                              Worker (background asyncio.Task)
(EventAccepted)                                |
                                          Batch: drain up to N items
                                          or flush on timeout
                                               |
                                               v
                                          MongoDB (insert_many, ordered=False)
                                          source of truth
                                               |
                                          per-item results
                                          (BulkWriteError handling)
                                               |
                                          +----+----+
                                     succeeded       failed
                                     + deduped         |
                                          |         nack / DLQ
                                          v
                                     Elasticsearch (async_bulk)
                                     dual write, _id = idempotency_key
                                          |
                                     per-item results
                                     (status code classification)
                                          |
                                     +----+----+
                                succeeded    failed
                                  |       +--+--+
                                 ack   retryable  permanent
                                       (429/5xx)  (4xx)
                                          |         |
                                        nack       DLQ
                                          |
                                          |  On max retries exceeded
                                          |  OR nack fails (queue full)
                                          v
                                      DLQ (asyncio.Queue, bounded)

GET /health -----------------> Check MongoDB + ES + Redis connectivity
                               + queue depth, DLQ depth, worker liveness
GET /events -----------------> MongoDB (filtered query)
GET /events/stats -----------> MongoDB (aggregation pipeline)
GET /events/search ----------> Elasticsearch (full-text search)
GET /events/stats/realtime --> Redis cache --miss--> MongoDB aggregation
                                                         |
                                                    cache result
```

All writes go through the queue. The POST handler never touches MongoDB or Elasticsearch directly; it validates the input, enqueues the event, and returns 202 Accepted. The worker is the sole writer to both stores. Read endpoints query whichever store is appropriate: MongoDB for structured queries and aggregation, Elasticsearch for full-text and metadata search, Redis for cached realtime stats.

## 2. Component Responsibilities

**API layer (FastAPI routers)** owns HTTP concerns only: input validation via Pydantic, response serialization, status codes. POST returns 202 with the validated input echoed back (no persistence fields). GET endpoints return full documents from the appropriate store. Route handlers are thin -- they validate input, call the service layer, and return the response.

**Queue (`asyncio.Queue`)** decouples ingestion from processing and provides backpressure. When the bounded queue is full, POST returns 503. The queue protocol (enqueue, drain, ack, nack) is a transport abstraction -- batch assembly via `drain` is a transport concern because `asyncio.Queue` and SQS have fundamentally different optimal strategies for it. Swapping to SQS means adding a new protocol implementation; the worker does not change.

**Worker (background `asyncio.Task`)** owns the entire write path to both stores. It drains batches from the queue, performs bulk writes to MongoDB (`insert_many`, `ordered=False`), classifies per-item results from `BulkWriteError`, forwards succeeded items (including deduplicated ones) to Elasticsearch via `async_bulk` with each document's `_id` set to `idempotency_key`, classifies ES per-item results by status code, and handles retry/DLQ routing. It also manages batch-cycle exponential backoff on connection-level failures.

**MongoDB** is the source of truth for event data. It serves structured queries (GET /events with filters), aggregation pipelines (GET /events/stats, GET /events/stats/realtime), and enforces event deduplication via a unique index on `idempotency_key`. Indexes are created at startup via `create_index()` (idempotent).

**Elasticsearch** is a search index derived from MongoDB, not authoritative. It enables full-text search across event metadata and flexible metadata queries that MongoDB cannot efficiently serve on arbitrary nested structures. Documents are keyed by `idempotency_key` (`_id`), which makes ES writes idempotent (retries overwrite, not duplicate) and provides a stable correlation key back to the authoritative MongoDB document. Two metadata representations serve complementary roles: `flattened` for structured key-based queries ("I know the key"), `metadata_text` (leaf-value extraction with standard analyzer) for full-text search ("search for a value somewhere in the metadata"). The ES index is created at startup with an explicit mapping to prevent dynamic mapping from overriding the intended field types.

**Redis** serves two roles: caching realtime stats responses with configurable TTL, and backing the rate limiting middleware with sliding window counters. Redis is ephemeral -- its loss is degradation, not failure. Cache misses fall through to MongoDB; rate limiting fails open.

## 3. Storage Rationale

**MongoDB as source of truth.** Events need durable, consistent storage with strong write guarantees. MongoDB provides ordered/unordered bulk writes with per-item error reporting, unique index enforcement for deduplication, and a rich aggregation framework for analytics queries. It was chosen over Elasticsearch as the authority because of stronger consistency guarantees and the ability to serve both write and read workloads reliably. The unique index on `idempotency_key` is the deduplication mechanism -- no Redis dependency in the write path, no TTL window, no race conditions.

**Elasticsearch for search.** The assignment requires full-text search across event metadata, which is arbitrary nested JSON. MongoDB cannot efficiently index or search arbitrary nested structures. Elasticsearch handles this with two field representations: a `flattened` type that indexes all metadata values as keywords (supporting term, prefix, range, exists queries), and a `metadata_text` field with the standard analyzer for full-text search. ES is a derived index -- if it falls behind or loses data, the source of truth in MongoDB is unaffected, and the search index can be rebuilt.

**Redis for cache and rate limiting.** Sub-second reads for cached realtime stats and atomic counter operations for rate limiting. Redis is not a durable store and the system does not depend on it for correctness. The realtime stats endpoint is approximate by nature; stale data within the TTL window is acceptable. The `RealtimeStatsQuery` schema constrains the parameter space (window enum + optional event\_type filter) to keep the total key space small and cache hit rates high.

**Standard MongoDB collections over time-series collections.** Standard collections provide simpler aggregation pipeline semantics and unrestricted document operations. Time-series collections offer storage compression and throughput gains but impose restrictions on updates/deletes and have different aggregation behavior. They are a consideration for production deployments where storage compression becomes material (see section 7).

## 4. Failure Modes

| Failure | Impact | Degradation |
|---------|--------|-------------|
| **MongoDB down** | Writes fail, worker backs off (per-batch-cycle exponential backoff, base 2s, max 60s, jitter) and retries. GET /events and GET /events/stats return 503. | Core functionality unavailable. Health reports `unhealthy`. |
| **Elasticsearch down** | ES portion of dual write fails (5xx). MongoDB writes succeed. Items nacked and retried -- on retry, MongoDB dedup succeeds (cheap unique-index lookup), ES gets another attempt (idempotent via `_id`). If ES stays down, items exhaust retries and route to DLQ. GET /events/search returns 503. | Search unavailable, all other endpoints functional. Health reports `degraded`. |
| **ES mapping conflict** | ES rejects specific items with 4xx (permanent). Items routed to DLQ immediately, no retry. MongoDB writes already succeeded -- events are persisted, just not searchable. | Affected events missing from search. DLQ surfaces failures for investigation. |
| **Sustained ES pressure (per-item 429s)** | Items nacked and retried without delay. No per-item backoff in the in-memory implementation -- items cycle through the queue rapidly, may exhaust retry budget when a longer wait would have succeeded. MongoDB writes succeed. | Items route to DLQ prematurely. Source of truth intact, but items potentially missing from search index. SQS visibility timeout eliminates this (redelivery timing per-message, independent of worker speed). |
| **Redis down** | Cache misses fall through to MongoDB. Rate limiting fails open. | Higher MongoDB load on realtime stats, no rate limiting. Health reports `degraded`. |
| **Redis down + thundering herd** | All realtime stats requests hit MongoDB simultaneously when cache layer disappears. | Production mitigation: circuit breaker -- after N Redis failures in a window, short-circuit to MongoDB for a cooldown period rather than attempting Redis on every request. |
| **Worker crash (process dies)** | In-flight batch and queued messages lost. | Events accepted via POST are lost. Documented trade-off of in-process queue -- SQS survives process crashes. |
| **Worker stall** | Events accepted but never processed. Queue fills, eventually POSTs return 503. | Health reports `unhealthy` (worker-alive flag stale). |
| **Queue full (backpressure)** | POST returns 503. No data loss for events not yet accepted. | Clients retry with backoff. Health reports `degraded`. |
| **Queue full + nack contention** | New POSTs fill the queue during batch processing. Nack fails to re-enqueue (`QueueFull`), item routed to DLQ instead of retried. | Retry opportunity lost, but MongoDB likely already has the event. SQS eliminates this -- `change_message_visibility(0)` operates on independent capacity. |
| **DLQ full** | Failed messages logged and dropped. | Source of truth (MongoDB) may already have the event. Failure details in structured logs. Health reports `degraded`. |
| **Graceful shutdown + dead worker** | Sentinel enqueue times out. Events in the queue are lost. Shutdown proceeds with warning log including queue depth. | Same as worker crash -- in-memory queue contents not recoverable without a live worker. |

## 5. Scaling Considerations

What breaks at 10x event volume and how to address it:

**Single worker.** The background `asyncio.Task` is a throughput ceiling. Under high load, the worker's batch processing rate determines maximum sustained ingestion. Address by running multiple worker tasks consuming from the same queue, or move to SQS with horizontally scaled consumers across processes/containers.

**Batch size tuning.** Larger batches amortize bulk write overhead (fewer round trips to MongoDB and ES) but increase per-batch latency and the blast radius of a failed batch. The optimal batch size depends on event payload size, network latency to the stores, and acceptable end-to-end latency. This needs profiling under realistic load.

**In-process queue.** Bounded by process memory, lost on crash. At scale, queue depth under backpressure becomes a memory concern. SQS provides durability, independent scaling, and eliminates the nack contention and per-item retry backoff limitations of the in-memory implementation.

**Single ES index.** A single index grows without bound, degrading search performance over time (larger segments, more expensive merges, slower queries). Production would use data streams with ILM policies for time-based index rotation -- new indices created on a schedule, old indices rolled over, aged indices deleted. This also provides natural retention management.

**MongoDB single-node.** For this scope, a single MongoDB instance. At scale, shard by `event_type` or `timestamp` range. The compound index on `(event_type, timestamp)` supports the most expensive query pattern (stats aggregation grouped by type and time bucket).

**Redis single-node.** Cache scalability via Redis Cluster or read replicas. Rate limiting counters may need distributed coordination if spread across multiple Redis nodes.

**Offset-based pagination.** `skip`/`limit` (MongoDB) and `from`/`size` (ES) degrade at depth -- MongoDB scans and discards skipped documents, ES has a default 10,000-hit limit on `from`/`size`. Production would use keyset/cursor-based pagination: last document's `_id` or `(timestamp, _id)` as a cursor for MongoDB, `search_after` for ES. The existing indexes already support cursor-based queries, so the change is in the API and query layer, not the storage layer.

## 6. Data Retention

**MongoDB TTL index.** A TTL index on `created_at` provides automatic document expiry with a configurable retention period. `created_at` is set in the service layer at enqueue time (when the system accepts the event), not at persistence time. This means the expiry window starts from when the system took responsibility for the event, not from when the worker happened to process it -- under backpressure, events may sit in the queue before the worker persists them, and the TTL should reflect acceptance time.

**Elasticsearch retention.** Under the current dual-write approach, ILM policies with data streams handle index rotation and deletion on a time-based schedule. Under the CDC approach (change streams, see section 7), MongoDB TTL deletes propagate as delete events through the change stream -- ES cleanup is automatic, and no separate ILM configuration is needed.

**Coordination rule.** ES retention must be greater than or equal to MongoDB retention. If MongoDB expires a document before ES deletes its corresponding index entry, search results will contain ghost references pointing to documents that no longer exist in the source of truth.

## 7. What Would Change in Production

**Queue: SQS.** The `EventQueue` protocol abstraction makes this a swap, not a rewrite. `drain` maps to `receive_messages(MaxNumberOfMessages, WaitTimeSeconds)`, `ack` to `delete_message`, `nack` to `change_message_visibility(0)`. SQS provides durability (survives process crashes), at-least-once delivery, and horizontal scaling. It also eliminates several limitations of the in-memory implementation: native redelivery timing via visibility timeout replaces the per-batch-cycle backoff sleep and resolves the per-item retry backoff limitation; independent queue capacity eliminates nack-vs-enqueue contention; the graceful shutdown sentinel is unnecessary since unprocessed messages remain in the queue for redelivery.

**Data sync: CDC via MongoDB change streams.** Replaces the dual write for decoupled, eventually-consistent sync between MongoDB and Elasticsearch. ES indexing is handled by a change stream consumer with resume tokens for fault recovery. This eliminates the redundant MongoDB round-trip on ES retry (currently, every ES retry incurs a MongoDB unique index lookup because the item goes through the full write path again). It also simplifies data retention: MongoDB TTL deletes propagate as delete events through the change stream, so ES cleanup is driven by the source of truth rather than a separate ILM policy.

**Rate limiting: infrastructure-level.** The custom Redis-backed sliding window middleware demonstrates ASGI middleware design and Redis atomic operations, but IP-based rate limiting is ineffective behind a reverse proxy or load balancer where all traffic shares the source IP. Production rate limiting belongs at the infrastructure level (API gateway, WAF, ingress controller) where real client IPs are visible.

**Health probes: Kubernetes liveness and readiness.** The current single health endpoint would be split into a liveness probe (cheap, no dependency checks, just return 200) and a readiness probe (check MongoDB connectivity; ES/Redis failures handled at the application level with per-endpoint 503s). The current `unhealthy`/`degraded` classification maps naturally: `unhealthy` fails readiness (pod removed from rotation), `degraded` still passes readiness (pod continues serving traffic).

**Observability.** Metrics via Prometheus (queue depth, DLQ depth, batch processing latency, per-store write latency, error rates by classification). Distributed tracing via OpenTelemetry (trace an event from POST through queue, worker, MongoDB, ES). Alerting on DLQ depth and worker lag.

**Time-series collections.** MongoDB 8.0 time-series collections offer roughly 16x storage compression and 2-3x throughput for append-mostly workloads. The trade-offs are restricted update/delete operations and different aggregation behavior. Worth evaluating once event volume reaches a level where storage compression becomes material.
