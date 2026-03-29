# Design Review — 2026-03-29-event-platform-design-v3.md

Reviewed 2026-03-29. Six issues identified, all resolved.

---

## 1. Per-message exponential backoff has no enforcement mechanism in the in-memory queue

**Issue:** The worker lifecycle (step 7) specifies "nack with exponential backoff (base 2s, max 60s, jitter)" per message, and `QueueMessage` carries a `retry_count` field that implies per-message tracking. But `asyncio.Queue` has no delayed delivery — when a message is nacked, it's re-enqueued and immediately available for the next `drain` call. The worker picks it up in the next batch cycle, which could be milliseconds later under low load. A message encountering a retryable failure burns through all 5 retries in seconds and gets DLQ'd before the service has time to recover.

**Decision:** Backoff is per-batch-cycle, not per-message. When a batch encounters retryable failures, the worker sleeps before the next `drain` call, using a consecutive-batch-failure counter (not per-message `retry_count`) to determine the delay. When a batch succeeds, the counter resets. The per-message `retry_count` continues to drive DLQ routing (max retries exceeded) but not timing. Note explicitly that SQS handles redelivery timing natively via visibility timeout, so the per-batch sleep is an in-memory-only concern that gets discarded on the SQS swap.

**Alternatives considered:**
- *Per-message delay buffer (heapq-based):* A structure in `retry.py` that holds nacked messages until their backoff period expires, releasing them to the main queue only after `2^retry_count` seconds. Correct per-message behavior, but adds a component that gets discarded on the SQS swap (SQS visibility timeout handles this natively). Rejected — complexity not justified when it won't carry forward.
- *Nack with visibility timeout wrapper:* Wrap `asyncio.Queue` with an in-flight dict and background reaper, mirroring SQS semantics. The research doc describes this pattern. Most correct analog to SQS behavior, but partially reimplements what SQS provides. Rejected — the design's philosophy is to keep the in-memory implementation simple and document the SQS gap, not build a partial SQS reimplementation.
- *Document the gap, accept immediate retry:* State that the in-memory queue does not enforce backoff timing. Honest, but a retryable failure spinning through 5 retries in seconds is wasteful and avoidable with ~5 lines of code. Rejected.

**Rationale:** Per-batch-cycle backoff addresses the behavior that actually matters: if MongoDB or ES is down, the worker backs off instead of spinning. The "healthy messages delayed" downside is largely theoretical — if the downstream store is returning errors, new messages would fail too. The SQS swap path stays clean: SQS handles redelivery timing natively, so the per-batch sleep doesn't carry over.

---

## 2. POST /events API contract is incomplete

**Issue:** The design specifies that `POST /events` validates input and enqueues for async processing, but the response contract is undefined. `EventResponse` "adds `id` and `created_at`," but neither value exists at enqueue time — `_id` is assigned by MongoDB and `created_at` is set at insertion. The design can't return `EventResponse` from POST without misrepresenting the event's state. The status code (201 vs 202), response body, and client expectations after receiving a response are all unspecified.

**Decision:** POST returns `202 Accepted` with an `EventAccepted` schema that echoes the validated input fields including `idempotency_key`. No `id`, no `created_at` — the response honestly represents what's known (accepted for processing, not yet persisted). No changes to the read-side schemas (`EventFilter`, `EventResponse`) — the producers writing events and the consumers querying events are different clients with different needs; producers are fire-and-forget, consumers filter by event_type, user_id, and date ranges.

**Alternatives considered:**
- *202 Accepted with `idempotency_key`, add `idempotency_key` to `EventFilter`:* Gives producers an explicit lookup path to verify processing. But the producers (systems firing pageview/click/conversion events) and consumers (analysts/dashboards querying) are different clients. Producers are fire-and-forget; adding a producer-oriented field to a consumer-oriented schema serves a use case that likely doesn't exist in this domain. Rejected.
- *202 Accepted with queue `message_id`, add status endpoint:* Return the `QueueMessage.message_id` and add `GET /events/status/{message_id}` for processing status. Gives real-time processing visibility, but requires tracking message state beyond the queue. Rejected — over-engineered for fire-and-forget web events.
- *Synchronous write, return 201 Created with full `EventResponse`:* Write to MongoDB in the handler, skip the queue. Complete and honest response, but contradicts the core design decision ("all writes go through the queue") and the assignment's requirement to demonstrate async ingestion pipeline design. Rejected.

**Rationale:** 202 is the correct status code — the request is accepted for processing but not completed. Echoing the validated input confirms "we received exactly this" without fabricating persistence state. The `idempotency_key` in the response lets producers track what they've sent for their own bookkeeping (e.g., not re-sending).

---

## 3. Graceful shutdown may lose queued-but-not-batched events

**Issue:** Worker lifecycle step 10: "sentinel value sent via lifespan shutdown, worker flushes remaining batch then exits." The uvicorn shutdown sequence is: stop accepting connections → complete in-flight requests → trigger lifespan shutdown. In-flight POST handlers can still enqueue events after the worker's last `drain` call but before the sentinel is enqueued. "Flushes remaining batch" is ambiguous — it could mean the worker continues draining until it encounters the sentinel (no loss), or it finishes only the current in-progress batch and exits (items queued between the last drain and the sentinel are lost). With an in-memory queue, lost events are gone permanently. Shutdown is supposed to be the graceful path — losing events during a clean deployment is a different category than losing them on crash.

**Decision:** Specify drain-until-sentinel behavior. The worker loop continues normally — drain, process, drain, process — until a drain result contains the sentinel. Items from that final drain that preceded the sentinel are processed as a normal batch. Then the worker exits. No events are lost during graceful shutdown.

**Alternatives considered:**
- *Two-phase shutdown:* Lifespan shutdown first signals the API to reject new POSTs (503), waits for the worker to drain the queue to empty, then sends the sentinel. Guarantees empty queue before worker stops. Rejected — adds coordination between the API layer and worker, and the "wait for empty" needs a timeout for the case where the queue never empties (persistent write failures). The sentinel approach already provides the ordering guarantee naturally.
- *Document the loss window, accept it:* The in-memory queue already loses everything on crash. Losing a few events on shutdown is a smaller version of the same limitation. Rejected — the crash data-loss is unavoidable without an external queue, but the shutdown data-loss is avoidable with zero additional code. The sentinel ordering guarantee (everything enqueued before the sentinel is drained before the sentinel is encountered) provides this for free.

**Rationale:** This is likely what the design intends but doesn't state precisely. Uvicorn completes in-flight requests before triggering lifespan shutdown, and lifespan shutdown enqueues the sentinel. The ordering is: last in-flight POSTs enqueue → sentinel enqueues → worker drains everything in FIFO order. The guarantee falls out naturally from the queue's ordering; the design just needs to state it explicitly.

---

## 4. Stats vs. realtime stats endpoint distinction is underspecified

**Issue:** Both `GET /events/stats` and `GET /events/stats/realtime` run MongoDB aggregation pipelines, sharing the same `StatsQuery` schema (`time_bucket` enum, optional `event_type` filter, date range). The difference is that the realtime endpoint has a Redis cache in front, with key `stats:realtime:{hash of query params}`. With the full `StatsQuery` parameter space — arbitrary date ranges especially — the number of unique cache keys is large. Most entries expire within the 30-second TTL before they're hit again. The cache adds latency to misses (Redis write on the way back) without offsetting it with hits.

This also raises the question of why two endpoints exist. If they accept the same parameters, a client has no reason to use `/stats` over `/stats/realtime` — the cached version is strictly better when it hits. And if it almost never hits, they're functionally identical.

The original assignment describes the realtime endpoint as a "lightweight stats summary" — the word "summary" suggests a fixed or constrained view, not an arbitrary-parameter query.

**Decision:** Constrain the realtime endpoint's parameters with a separate, narrower schema. Replace arbitrary date ranges with a `window` enum (e.g., last 1h, 6h, 24h). Optional `event_type` filter stays. Time bucket is derived from the window (e.g., per-minute for 1h, hourly for 24h) rather than client-specified. This gives a parameter space of maybe a few dozen combinations — high cache hit rates when dashboards poll the same view.

The unconstrained `/stats` endpoint keeps the full `StatsQuery` for ad-hoc analytical queries where caching wouldn't help.

**Alternatives considered:**
- *Keep shared parameters, document low hit rate:* Acknowledge the cache is most effective when clients converge on common queries. Honest, but leaves a component that doesn't accomplish much for most usage patterns, and a reviewer would rightly question the caching design. Rejected.
- *Remove the realtime endpoint, add caching as middleware on `/stats`:* A single endpoint with optional cache behavior. Avoids the two-endpoint confusion but conflates two different access patterns and makes the caching demonstration less visible. Rejected.
- *Fixed query, no parameters at all:* The realtime endpoint always returns the same thing (e.g., last hour by event type). Guaranteed cache hits, but too rigid — a small amount of parameterization costs very little hit rate but makes the endpoint meaningfully more useful. Rejected.

**Rationale:** The two endpoints should serve genuinely different purposes reflected in their schemas. The constrained realtime schema also produces a more interesting caching discussion for ARCHITECTURE.md: why the parameters were limited (hit rate), why TTL-only invalidation works (small key space, approximate data), and how higher write volume pressures the TTL.

---

## 5. ES bulk failure handling doesn't distinguish retryable from permanent errors

**Issue:** Worker step 6: "ES failure for an item = nack." The MongoDB side has explicit error classification (step 4: error code 11000 = treat as success, other codes = nack/DLQ). The ES side treats all per-item failures identically.

ES `async_bulk` returns per-item results with status codes: 400 (malformed document, mapping conflict) is permanent — retrying won't help; 429 (rate limited) and 503 (shard unavailable) are retryable. Without this distinction, a document that fails ES mapping validation cycles through all 5 retries, each incurring a wasted MongoDB dedup round-trip, before finally being DLQ'd. Under a bad data scenario, this multiplies wasted work by the retry count across every affected item.

**Decision:** Classify ES per-item failures by status code. 4xx (except 429) → DLQ immediately, no retry. 429 and 5xx → nack for retry. This mirrors the MongoDB error code classification pattern already established in the design.

**Alternatives considered:**
- *Classify into three buckets via shared enum:* Formalize success/retryable/permanent as a classification structure shared between MongoDB and ES result handlers. Adds a small abstraction for two call sites. Rejected — unnecessary structure at this scale.
- *Keep uniform nack, rely on max retries:* 5 retries is bounded cost. Permanent failures burn through retries quickly and DLQ regardless. Rejected — inconsistent with the care taken on the MongoDB side. A reviewer seeing detailed `BulkWriteError` classification for MongoDB and blanket nack for ES would question the asymmetry.
- *Log and drop ES permanent failures without DLQ:* If MongoDB succeeded, the event is persisted — just not searchable. Don't consume DLQ capacity. Rejected — the DLQ exists to surface failures for investigation. Systematic ES mapping errors should be visible, not silently swallowed.

**Rationale:** The principle of per-item error classification is already established on the MongoDB side. Extending it to ES is a conditional in the bulk result loop, not a new mechanism. The implementation is straightforward: check the status code, route accordingly.

---

## 6. `metadata_text` field transformation is unspecified

**Issue:** The ES mapping defines `metadata_text` as a `text` field for full-text search across metadata values, with the note that "this is handled in the worker's serialization step." The transformation from nested metadata dict to searchable text is not specified.

Different transformations produce different search behavior. For `{"browser": {"name": "Chrome", "version": 120}}`:
- JSON serialization: `'{"browser": {"name": "Chrome", ...}}'` — the standard analyzer tokenizes structure, so "browser" (a key) matches as text. Keys are noise in the full-text index.
- Leaf-value extraction: `"Chrome 120"` — only values are searchable.
- Key-value pairs: `"browser name Chrome browser version 120"` — keys and values both searchable, muddles the separation between the two metadata fields.

The choice determines what's searchable. The `flattened` field already provides exact key-based queries (`metadata.browser.name: Chrome`). The `metadata_text` field exists because the assignment requires "full-text search across event metadata" — the `flattened` type provides keyword-level queries but not full-text analysis (tokenization, case folding). The two fields should have complementary, non-overlapping roles.

**Decision:** Recursive leaf-value extraction. Walk the metadata dict, collect all leaf values (strings; numbers converted to string), concatenate with spaces. Keys are excluded — they're queryable via the `flattened` field. This gives a clean separation: `metadata` (flattened) for structured "I know the key" queries, `metadata_text` for unstructured "I'm searching for a value somewhere in the metadata" queries.

**Alternatives considered:**
- *JSON serialization:* Simplest (one function call), but indexes structural noise. Searches for common key names like "type" or "name" match broadly across unrelated events, polluting full-text results. Rejected.
- *Key-value pair concatenation:* Richer than leaf-only but redundant with the `flattened` field's key-based queries. Adds index size without a clear use case for full-text searching key names. Rejected.
- *Leave unspecified:* The mapping and intent are documented; the transformation is an implementation detail. Rejected — the transformation affects observable search behavior and is the kind of decision that should be explicit in the design rather than left to implementer interpretation.

**Rationale:** Leaf-value extraction is the only option where the two metadata fields have clearly complementary, non-overlapping purposes. It follows directly from the design's own two-field rationale: `flattened` for structure-aware queries, `text` for value-oriented full-text search.
