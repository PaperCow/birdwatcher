# Design Review — 2026-03-29-event-platform-design-v2.md

Reviewed 2026-03-29. Five issues identified, all resolved.

---

## 1. `insert_many` requires `ordered=False` for per-item ack/nack

**Issue:** The worker's batch model relies on `insert_many` returning per-item results so each event can be individually acked or nacked. But `insert_many` defaults to `ordered=True`, which stops at the first error — if item 3 of 10 hits a `DuplicateKeyError`, items 4–10 are never attempted. The per-item ack/nack model only works with `ordered=False`.

Additionally, the error handling mechanism needs to be explicit: with `ordered=False`, PyMongo raises `BulkWriteError` when any items fail. The exception's `details['writeErrors']` contains per-item failures with their batch index and error code. Success is inferred by exclusion — any index not in `writeErrors` succeeded. The ES bulk write should only be attempted for items that succeeded in MongoDB (plus duplicates treated as success), not the full original batch.

**Decision:** Specify `ordered=False` for `insert_many`. Document the `BulkWriteError` handling pattern: build a set of failed indexes from `writeErrors`, classify by error code (11000 = duplicate = treat as success, others = nack/DLQ), and pass only succeeded items to the ES bulk write.

**Alternatives considered:**
- *Keep `ordered=True`, handle the short-circuit:* On any error, treat all subsequent items as "not attempted" and nack them. Preserves insertion order but a single duplicate in a batch forces re-processing of everything after it. Rejected — wasteful and unnecessarily complex.
- *Pre-deduplicate batches before `insert_many`:* Query idempotency keys before the bulk write. Adds a round-trip and introduces a TOCTOU race (event inserted between check and write), so duplicate handling is still needed. Rejected — doesn't eliminate the problem and adds cost.

**Rationale:** `ordered=False` is the only option where the implementation matches the per-item result model. Insertion ordering is irrelevant — events are independent and carry their own timestamps. The `BulkWriteError` handling pattern is the standard approach for mixed-result batch processing with PyMongo.

---

## 2. ES failure retry semantics are ambiguous

**Issue:** Three statements in the design describe different behavior when MongoDB succeeds but ES fails for an item: section 1 says ES failures are "retried per-item," section 4 says ack/nack is based on "individual success/failure from bulk results," and the data consistency section says ES failure is "logged for investigation." These are mutually inconsistent — either ES failure triggers a nack (retry), or it's accepted and logged (no retry).

**Decision:** Nack on ES failure, retry through the main queue. The item re-enters the queue, gets picked up in a future batch, MongoDB hits `DuplicateKeyError` (treated as success per dedup logic), and ES gets another attempt. Under the production CDC architecture (change streams), this dual-write retry problem disappears entirely — ES indexing is decoupled from the write path and retry semantics are handled by the change stream consumer's resume tokens.

**Alternatives considered:**
- *Ack on ES failure, log and accept the gap:* Simple, but means there's no recovery path for ES gaps short of a full reindex. A brief ES outage would permanently lose search coverage for affected events. Rejected — too lossy for the search index.
- *Ack on ES failure, retry ES writes separately:* Collect failed ES items into a separate retry buffer and retry directly without re-enqueuing. Avoids the wasted MongoDB round-trip, but introduces a second retry path with its own backoff/DLQ logic — duplicating the retry infrastructure. Rejected — complexity not justified at this scope.

**Rationale:** The redundant MongoDB duplicate check on retry is cheap (unique index lookup, returns immediately), and the approach reuses existing retry/DLQ infrastructure with no new mechanisms. The ack/nack rule becomes explicit: nack if either MongoDB or ES fails for that item. `DuplicateKeyError` on retry is expected and treated as success. This is a known trade-off of the dual-write approach that the production CDC path eliminates.

---

## 3. ES down classified as "unhealthy" but behaves like "degraded"

**Issue:** The health endpoint defines `unhealthy` as "MongoDB or ES unreachable." But the rest of the design describes ES down as partial degradation: ingestion works, events are stored in MongoDB, stats work, realtime stats work — only search is unavailable. The failure modes table confirms: "Search unavailable, all other endpoints functional." Redis down gets `degraded` for the same reason (core functionality works). ES down is arguably less impactful than Redis down (which affects both caching and rate limiting).

**Decision:** ES down = `degraded`, not `unhealthy`. Reserve `unhealthy` for MongoDB only — the sole dependency whose loss prevents the system's primary function (ingesting and storing events). This aligns with Kubernetes readiness probe best practice: MongoDB down would fail readiness (pod removed from rotation), while ES or Redis down would still pass readiness (pod continues serving traffic, affected endpoints return 503 individually).

**Alternatives considered:**
- *Keep ES down as `unhealthy`:* Could argue the system isn't "healthy" without search. But this contradicts the design's own degradation model and treats a derived search index the same as the source of truth. Would also cause problems under Kubernetes — all pods failing readiness when ES is down means zero traffic served, which is worse than serving everything except search. Rejected.
- *Introduce a third level (e.g., `limited`):* More granular but adds complexity for minimal benefit at this scope. Rejected.

**Rationale:** The health classifications should reflect the actual impact hierarchy. MongoDB is the source of truth and the only hard dependency. ES and Redis are derived/ephemeral — their loss degrades specific features but doesn't prevent the system from fulfilling its primary purpose.

---

## 4. Queue protocol doesn't support batch drain

**Issue:** The `EventQueue` protocol defines `dequeue() -> QueueMessage` for single items, but the worker needs to "drain up to N items or flush on timeout since first item." The protocol interface doesn't support this pattern. The drain logic is also non-trivial: the first item should block until something arrives, then subsequent items should be grabbed non-blocking until the batch is full or the queue is empty. The single `dequeue()` doesn't distinguish between these modes.

This also matters for the SQS swap path — SQS's `receive_messages` natively supports `MaxNumberOfMessages` and `WaitTimeSeconds` in a single call, which maps directly to a batch drain method but not to repeated single-item dequeues.

**Decision:** Replace `dequeue()` with `drain(max_items: int, timeout: float) -> list[QueueMessage]` in the protocol. The queue owns the batch drain logic. For the in-memory implementation, it handles the blocking-first-then-nonblocking pattern internally. For SQS, it maps directly to `receive_messages` parameters.

**Alternatives considered:**
- *Keep single-item protocol, build drain logic in the worker:* The worker calls `dequeue` with a timeout for the first item, then loops with zero-timeout for the rest. Simpler protocol, but the worker now contains transport-specific timing logic that would need to change for SQS (where a single API call is both cheaper and more efficient than N individual receives). Rejected — defeats the purpose of the protocol abstraction.
- *Add both `dequeue` and `drain` to the protocol:* More surface area, and nothing in the design uses single-item dequeue. Rejected — if needed later, it's trivial to add back.

**Rationale:** The protocol abstraction exists so the worker doesn't know what the transport is. Batch drain is a transport concern — `asyncio.Queue` and SQS have fundamentally different optimal strategies for it. Putting that logic behind the protocol keeps the worker clean and makes the SQS swap genuinely mechanical.

---

## 5. Rate limiting middleware applies to health checks, and IP-based rate limiting is ineffective in containerized environments

**Issue:** Two related problems. First, the rate limiter is ASGI middleware applied globally before routing, meaning `GET /health` (the Docker health check) is rate-limited — under load, it could be rejected with 429, causing false health check failures. Second, IP-based rate limiting is largely ineffective in containerized environments where all requests arrive from the same source (load balancer, ingress controller, reverse proxy). The rate limiter would rate-limit the infrastructure, not the clients.

**Decision:** Exclude the health endpoint from rate limiting via a path check in the middleware. Reframe the rate limiter in the design as a demonstration of Redis-backed ASGI middleware design, not a functional rate limiting solution. The design already notes infrastructure-level rate limiting (API gateway, WAF) as the production approach — make this framing explicit alongside the middleware description, not just in the ARCHITECTURE.md production section.

**Alternatives considered:**
- *Apply rate limiting per-router instead of globally:* Attach to `events`, `analytics`, and `search` routers only; health excluded by omission. Cleaner separation, but changes the middleware design for a problem solved with two lines of code. Rejected — over-engineered for the fix needed.
- *Make exclusion list configurable:* `config.py` gets `rate_limit_exclude_paths` defaulting to `["/health"]`. More flexible, but adds configuration for something unlikely to have more than one or two entries. Rejected — unnecessary abstraction.
- *Rate limit by authenticated identity instead of IP:* More effective in containerized environments, but the system has no authentication. Not applicable.

**Rationale:** The value of the custom middleware is demonstrating ASGI middleware design and Redis atomic operations, not providing production-grade rate limiting. Being explicit about this shows architectural awareness — a reviewer seeing an IP-based rate limiter behind a proxy would flag it as ineffective, but a rate limiter clearly framed as a pattern demonstration is a strength. Health endpoint exclusion is standard practice regardless.
