# Design Review — 2026-03-29-event-platform-design-v6.md

Reviewed 2026-03-29. Five issues identified, all resolved.

---

## 1. `created_at` timestamp — unspecified when it's populated

**Issue:** The MongoDB document includes `created_at` ("when ingested"), and ARCHITECTURE.md section 6 uses it for TTL-based data retention. But the design never specifies when this value is set — at POST validation time (when the API accepts the event) or at worker write time (when `insert_many` executes). Under backpressure, there could be meaningful lag between these. The choice affects TTL retention behavior: write-time `created_at` means events queued during a spike get a later expiry than events that moved through instantly, even though both were accepted at similar times.

**Decision:** `created_at` is set at enqueue time (in the service layer, before the event enters the queue) and carried as part of the `QueueMessage` payload. This gives the field a consistent meaning ("when the system accepted the event") independent of queue depth, and the TTL expiry window starts from acceptance, not persistence.

**Alternatives considered:**
- *Set `created_at` at worker write time:* Simpler — the worker sets the timestamp when building the MongoDB document, no need to carry it through the queue. But the timestamp then reflects persistence time, not acceptance time. Under backpressure, events queued for minutes would get a later `created_at` than their actual acceptance time. TTL expiry would be skewed — events that sat in the queue get a longer effective lifetime than events that moved through quickly. Rejected — the timestamp should mean one thing consistently.

**Rationale:** The event's `timestamp` (client-provided) represents when the event occurred. The system's `created_at` should represent when the system accepted responsibility for it — that's enqueue time, not write time. Carrying it through the `QueueMessage` is trivial (one additional field on a wrapper that already exists).

---

## 2. Graceful shutdown sentinel assumes the worker is alive

**Issue:** Step 10 states that a sentinel value is enqueued via the lifespan shutdown hook, guaranteeing "no events are lost during graceful shutdown." This guarantee implicitly requires the worker to be running and draining. If the worker has crashed and the queue is full, a blocking `put()` deadlocks the shutdown process — the worker is the only consumer and nothing will drain the queue. A non-blocking `put_nowait()` raises `QueueFull`, and the sentinel never enters the queue. This isn't a process crash (where everything is lost anyway) — it's a graceful shutdown where the shutdown process itself stalls.

**Decision:** Enqueue the sentinel with a timeout: `asyncio.wait_for(queue.put(sentinel), timeout=N)`. If the timeout expires, the worker is presumed dead — log a warning with the current queue depth (those events are lost) and proceed with shutdown. Explicitly state that the "no events lost during graceful shutdown" guarantee requires the worker to be alive. SQS eliminates this concern entirely — shutdown means the worker stops polling, unprocessed messages remain in the queue with their visibility timeout and are redelivered.

**Alternatives considered:**
- *Cancel the worker task directly instead of using a sentinel:* Set a shutdown flag, then `worker_task.cancel()` and `await worker_task`. Doesn't depend on the queue at all — works regardless of queue state. But `cancel()` raises `CancelledError` inside the worker, which could interrupt a batch mid-write (between MongoDB and ES writes). The worker would need to handle `CancelledError` at safe cancellation points, adding complexity. Rejected — the sentinel approach lets the worker finish its current batch naturally, and the timeout handles the dead-worker edge case.
- *Combine sentinel with cancel fallback:* Try sentinel with timeout. If timeout expires, cancel the worker task. Covers both cases (worker alive → clean drain; worker dead → cancel unblocks shutdown). But this is two shutdown mechanisms for a scenario that's already degraded (dead worker = events lost regardless). Rejected — over-engineered for the failure mode.
- *Document the assumption, no timeout:* State that graceful shutdown requires a live worker. The health endpoint reports `unhealthy` for a dead worker, triggering container restart. But this leaves a real hang risk in the shutdown process, which is worse than crashing. Rejected — the timeout costs one line and prevents the hang.

**Rationale:** The timeout is minimal implementation cost and prevents the shutdown process from hanging indefinitely. The assumption (worker must be alive) is already implicit — without a live worker, events in the queue are lost regardless of the sentinel. Making it explicit and handling the failure case cleanly is the proportionate response. SQS eliminates the entire concern since there's no sentinel — the worker simply stops polling.

---

## 3. Integration tests have no worker synchronization strategy

**Issue:** The testing section describes integration tests following a POST-then-query pattern: "POST event returns 202 → worker processes batch → GET /events returns it." But POST returns 202 before the worker processes the event — the event is only enqueued. Every integration test (`test_ingestion.py`, `test_search.py`, `test_stats.py`, `test_realtime_cache.py`) must wait for the worker to drain, batch, and write to both MongoDB and ES before asserting on GET. The design doesn't describe how tests synchronize with the async worker. Without this, tests are racy (assert too early), slow (arbitrary sleeps), or brittle (polling loops with retry logic in every test).

**Decision:** Use `await queue.join()` after POSTing events in integration tests. `asyncio.Queue.join()` blocks until all enqueued items have been `task_done()`'d by the worker — this is a precise, non-racy synchronization point with no polling or sleeping. Document as a test-only synchronization mechanism.

**Alternatives considered:**
- *Poll the GET endpoint in a retry loop:* Call GET with retries and short sleeps until the event appears. Works, but adds retry/timeout logic to every integration test. Tests become slower (cumulative sleep time) and harder to read. Also conflates "worker hasn't processed yet" with "the feature is broken" — a slow worker causes a test timeout that looks like a failure. Rejected — imprecise and noisy.
- *Bypass the queue in integration tests — call worker processing directly:* After POSTing, directly invoke the worker's batch-processing logic on the enqueued items. Gives precise control but tests a different code path than production — the real worker's drain/batch/flush loop is skipped. Integration tests exist to test the real end-to-end path. Rejected — defeats the purpose.
- *Use `asyncio.Event` signaling:* The worker sets an event after each batch completes. Tests wait on the event after POSTing. Works, but `queue.join()` already provides exactly this semantic using built-in asyncio.Queue functionality — no custom signaling needed. Rejected — reinvents what `join()` already does.

**Rationale:** `asyncio.Queue.join()` is a built-in synchronization primitive that does exactly what the tests need — wait until all enqueued items are processed. It requires no additional infrastructure, no polling, and no arbitrary timeouts. The worker already calls `task_done()` as part of `ack()`, so the join condition is naturally satisfied by the existing processing flow.

---

## 4. No bounds validation on client-provided `timestamp`

**Issue:** The design applies defensive validation to `metadata` (64KB max, 10 levels of nesting) as an external input boundary. But `timestamp` is also client-provided and has no validation beyond Pydantic's type check (is it a valid datetime?). A client can submit timestamps arbitrarily far in the past or future. For stats aggregation, this creates sparse/extreme time buckets — a `timestamp` of 2005-01-01 mixed with events in 2026 creates a 21-year range. For realtime stats, far-future timestamps could appear in "last 1 hour" windows when they shouldn't. This is the same class of concern as metadata validation: a pathological external input causing disproportionate downstream effects.

**Decision:** Add a Pydantic validator on `EventCreate.timestamp` rejecting timestamps beyond a configurable window — e.g., 30 days in the past, 5 minutes in the future (to accommodate client clock skew). Reject with 422 at the API boundary, same pattern as metadata validation. Bounds configurable via `config.py`.

**Alternatives considered:**
- *Filter out-of-range timestamps in the aggregation pipeline:* Add a `$match` stage that excludes timestamps outside a reasonable window. Doesn't reject bad data — it enters the pipeline, persists in MongoDB, and is silently excluded from stats. The event is still searchable in ES and visible in GET /events, but invisible in stats. Inconsistent behavior across endpoints. Rejected — fails late, creates confusing divergence between endpoints.
- *Clamp timestamps to the valid range instead of rejecting:* Replace out-of-range timestamps with the nearest boundary (e.g., future timestamps → now). Silently modifies client data, which violates the principle that the system either accepts or rejects input — never silently changes it. Rejected — surprising behavior for the client.

**Rationale:** Same defensive-validation principle as metadata limits. The API boundary is the right place to reject pathological input. The configurable window accommodates different deployment contexts (batch processing from clients with clock skew may need a wider past window). The 422 response gives the client a clear, actionable error.

---

## 5. `GET /events/stats` has no guardrails against expensive aggregations

**Issue:** The realtime stats endpoint deliberately constrains its parameter space (window enum, derived time bucket) for performance and cacheability. The general stats endpoint accepts arbitrary date ranges with any time bucket. An hourly-bucketed query spanning a year produces ~8,760 group documents from the aggregation pipeline. The compound index on `(event_type, timestamp)` covers the `$match` and `$group`, but the aggregation still materializes all groups in memory. MongoDB's aggregation memory limit (100MB default) would eventually cap this, but the failure is an opaque server error rather than a helpful client error.

**Decision:** Validate maximum date range per time bucket: hourly capped at 7 days, daily at 365 days, weekly at ~2 years. Reject with 422 at the API boundary. Limits configurable via `config.py`. Same philosophy as the realtime endpoint — constrain the parameter space for predictable performance.

**Alternatives considered:**
- *Add a `$limit` stage to the aggregation pipeline and return a truncation indicator:* Cap the number of returned buckets (e.g., 500) and include a `truncated: true` flag in the response. Allows wide queries but caps the cost. But the client gets partial data with no way to paginate the remainder — truncated aggregation results aren't useful for analytics. And the aggregation still does full work up to the limit stage. Rejected — partial results are worse than a clear rejection.
- *Use `allowDiskUse` to handle large aggregations:* Removes the 100MB memory limit by spilling to disk. Handles the symptom (opaque error) but not the cause — the aggregation is still expensive, just slower instead of failing. Disk-backed aggregations on large ranges could take seconds, impacting other queries. Rejected — trades a clear failure for a slow response.
- *No guardrails — rely on MongoDB's default limits:* The 100MB aggregation memory limit already caps pathological queries. But the error is opaque (MongoDB server error surfaced as 500), not actionable for the client. The design's own realtime endpoint demonstrates that constraining parameters is better than relying on server limits. Rejected — inconsistent with the realtime endpoint's design philosophy.

**Rationale:** The realtime endpoint already demonstrates the right approach: constrain the parameter space for predictable performance. The general stats endpoint should apply the same principle with wider but still bounded limits. Per-bucket caps are intuitive (hourly makes sense for days, not months) and configurable for different deployment contexts.
