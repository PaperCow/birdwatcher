# Design Review — 2026-03-29-event-platform-design.md

Reviewed 2026-03-29. Seven issues identified, all resolved.

---

## 1. Worker processes one event at a time — no batching

**Issue:** The worker loop processes one event per iteration (`dequeue → MongoDB write → ES write → ack`). The research explicitly flagged that single-document ES indexing is slow compared to `async_bulk`. For a system handling high-volume web events, this is the most significant throughput bottleneck in the design.

**Decision:** Add batch processing. Worker drains up to N items from the queue, then bulk-writes to MongoDB with `insert_many` and bulk-indexes to ES with `async_bulk`. Flush triggers on whichever comes first: batch size reached or timeout since first item in batch.

**Alternatives considered:**
- *Count-only trigger (no timeout):* Simpler, but if load drops to zero after a burst, the last partial batch sits unprocessed until the next event arrives. Rejected because stale data during quiet periods is avoidable with minimal added complexity.
- *Whole-batch ack/nack:* One failure fails the entire batch. Rejected — both `insert_many` and `async_bulk` return per-item results, so per-item ack/nack is feasible and avoids re-processing successful items.

**Rationale:** Client perspective is fire-and-forget (response comes when event hits the queue), so the timeout is about storage write latency, not client-perceived latency. A timeout fallback handles the quiet-period edge case without meaningful complexity cost.

---

## 2. ARCHITECTURE.md is a first-class deliverable but has no design

**Issue:** The requirements list ARCHITECTURE.md as a standalone deliverable with six required sections (system diagram, component responsibilities, storage rationale, failure modes, scaling considerations, what would change). The design references ARCHITECTURE.md three times as a place to document things but never plans the document itself — its structure, section content, or the failure-mode and scaling analyses.

**Decision:** Add a section to the design document that outlines the ARCHITECTURE.md structure, maps each required section to the content that populates it, and sketches the failure-mode and scaling analyses.

**Alternatives considered:**
- *Write ARCHITECTURE.md during implementation as things come up:* Rejected — the requirements treat it as a first-class deliverable, and the analytical sections (failure modes, scaling) should inform the design rather than describe it after the fact.

**Rationale:** The scaling analysis in particular would have naturally surfaced issue #1 (single-event worker throughput). Planning the document upfront ensures it's a design artifact, not an afterthought.

---

## 3. No application health endpoint

**Issue:** The docker-compose table defines health checks for MongoDB, ES, and Redis, but the app row shows "—". Docker has no way to determine if the app is ready to serve traffic. The design already describes degradation states (Redis down, ES failure) but has no mechanism to surface them.

**Decision:** Add a single `GET /health` endpoint that checks connectivity to MongoDB, ES, and Redis. Include it as the app's docker-compose health check. Document in ARCHITECTURE.md that production Kubernetes would split this into separate liveness (cheap, no dependency checks) and readiness (dependency connectivity) probes.

**Alternatives considered:**
- *Separate liveness and readiness endpoints now:* The Kubernetes pattern of `/health/live` (just return 200) and `/health/ready` (check dependencies). Decided against for this scope — a single endpoint is sufficient for Docker, and the Kubernetes split is noted for the production evolution discussion.

**Rationale:** The app already has defined degradation states. A health endpoint surfaces them and enables Docker health checking with minimal effort.

---

## 4. Unbounded dead letter queue

**Issue:** The DLQ is defined as an unbounded `asyncio.Queue`. Under sustained failures (e.g., ES down for an extended period), every event that exhausts retries goes into the DLQ with no upper bound on memory consumption.

**Decision:** Bound the DLQ. When full, log the dropped message and discard it.

**Alternatives considered:**
- *Write overflow to disk:* Adds I/O complexity and a new failure mode (disk full) for messages that are already logged failures. Rejected as over-engineered for this scope.
- *Block the worker when DLQ is full:* Would stall all processing, turning a partial failure into a total one. Rejected.
- *Keep unbounded, document the risk:* The risk is real (OOM under sustained failure) and easy to mitigate. Rejected.

**Rationale:** Messages reaching the DLQ have already failed processing and been logged. Dropping them under memory pressure is acceptable — the MongoDB write may have already succeeded (source of truth intact), and the failure is recorded in logs.

---

## 5. MongoDB time-series collections not considered

**Issue:** The design uses standard MongoDB collections with mongo:8, but the research found that MongoDB 8.0 time-series collections offer ~16x storage compression and 2-3x throughput for append-mostly workloads. Web events are textbook append-mostly data. The design doesn't acknowledge this option.

**Decision:** Keep standard collections. Document in ARCHITECTURE.md why, and flag time-series collections as a production consideration.

**Alternatives considered:**
- *Use time-series collections:* Restrictions on updates/deletes, different aggregation behavior, and less flexible indexing. The storage/throughput gains aren't needed at demonstration scale. Rejected for this scope.

**Rationale:** Standard collections have simpler aggregation pipeline semantics and no restrictions on updates. The design should acknowledge the alternative and explain the choice, especially since the ARCHITECTURE.md requires a storage rationale section. Also noted: under a future CDC/change-stream architecture, MongoDB TTL indexes would handle retention and propagate deletes to ES via change streams, eliminating the need for separate ES lifecycle management. Time-series collections' interaction with change streams would need evaluation at that point.

---

## 6. Custom rate limiter with no stated rationale

**Issue:** Every technology choice in the design's section 8 table has a justification except rate limiting. The design specifies a custom Redis-backed sliding window middleware but doesn't explain why over `fastapi-limiter` (recently updated) or `slowapi` (widely used but stale).

**Decision:** Add a row to the technology choices table. Rationale: simple custom implementation sufficient for demonstration scope; would revisit with a production-grade library or infrastructure-level rate limiting in a production environment.

**Alternatives considered:**
- *Use `fastapi-limiter`:* Recently updated, Redis-backed, dependency-based. Viable, but a custom implementation demonstrates middleware design and Redis usage, which is more relevant for a portfolio project.
- *Use `slowapi`:* Most widely used but stale (last release Feb 2024). Rejected due to maintenance risk.

**Rationale:** The choice is fine — it just needs to be stated alongside the other technology decisions for consistency.

---

## 7. ES index lifecycle — no rotation or retention strategy

**Issue:** The design defines an ES mapping on a single index that grows indefinitely. The research flagged time-based index strategies (data streams with ILM) as standard practice for event data. A single growing index degrades search performance over time and makes data retention management expensive.

**Decision:** Keep the single index. Document it as a scope simplification in the design and ARCHITECTURE.md. Note index rotation (data streams with ILM) and general data retention policies (MongoDB TTL indexes, coordinated ES cleanup) as production concerns.

**Alternatives considered:**
- *Implement ES data streams with ILM:* Moderate effort (index templates, ILM policy configuration, `@timestamp` field mapping, test fixture changes) but adds conceptual weight without demonstrating much beyond ES operational configuration. Rejected for this scope.

**Rationale:** Under the future CDC architecture (MongoDB change streams feeding ES), the retention story simplifies: MongoDB TTL indexes handle data expiry, delete events propagate to ES via change streams, and ES doesn't need its own lifecycle management. This end-state design is worth documenting even though the implementation stays simple.
