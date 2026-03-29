# Design Review — 2026-03-29-event-platform-design-v5.md

Reviewed 2026-03-29. Five issues identified, all resolved.

---

## 1. ES document ID strategy is unspecified — retries may create duplicates in Elasticsearch

**Issue:** The retry path for ES failures is carefully designed on the MongoDB side: nacked items re-enter the queue, MongoDB dedup via the unique index on `idempotency_key` succeeds, and the item proceeds to ES for another attempt. But the design never specifies what `_id` ES documents receive. If `async_bulk` uses auto-generated IDs, the items that already succeeded in the first partial ES write get duplicated on retry — there's no dedup mechanism on the ES side equivalent to MongoDB's unique index. This also means search results from ES have no stable identifier linking back to the authoritative MongoDB document for cross-store correlation.

**Decision:** Use `idempotency_key` as the ES document `_id`. Indexing with the same `_id` is an upsert in ES — retries overwrite rather than duplicate, making the ES write path idempotent without any additional mechanism. This also gives search results a natural correlation key back to the source of truth in MongoDB.

**Alternatives considered:**
- *Use MongoDB's `ObjectId` (as string) as ES `_id`:* Also provides idempotency and cross-store correlation. But the `ObjectId` is only available after the MongoDB write succeeds, which means the ES document `_id` can't be determined at enqueue time. `idempotency_key` is known from the moment the event arrives at the API boundary. Rejected — works but has an unnecessary dependency on MongoDB write ordering.
- *Rely on ES auto-generated IDs and accept potential duplicates:* The retry path is the only source of duplicates, and the volume of retried items is small relative to total throughput. Duplicates in ES affect search result counts but not the source of truth (MongoDB). But the dual-write retry path is a core design element — it would be inconsistent to carefully handle MongoDB idempotency and leave ES non-idempotent. Rejected — undermines the retry design's correctness guarantees.

**Rationale:** `idempotency_key` is already the event's identity for deduplication purposes. Using it as the ES `_id` extends the same concept across both stores — MongoDB enforces uniqueness via its unique index, ES enforces it via document `_id` semantics. No new fields, no new concepts.

---

## 2. Health endpoint is blind to async pipeline state

**Issue:** The health endpoint checks MongoDB, ES, and Redis connectivity and classifies the system as healthy/degraded/unhealthy. But the async pipeline — queue, worker, and DLQ — is invisible to it. The system could report `healthy` while the worker task has crashed, the queue is at capacity (all POSTs returning 503), or the DLQ is full and events are being dropped. Since this endpoint is the docker-compose health check, a stalled pipeline won't trigger any container-level response. The design defers full observability (Prometheus, OpenTelemetry) to production, but basic pipeline visibility is cheap and useful even during development.

**Decision:** Include queue depth, DLQ depth, and worker-alive status in the health endpoint response. Queue depth is `queue.qsize()`. DLQ depth is `dlq.qsize()`. Worker-alive is a boolean flag set by the worker on each loop iteration (e.g., a timestamp of the last successful drain, checked against a staleness threshold). Extend the `degraded` classification to include pipeline pressure signals — queue near capacity or DLQ non-empty. Worker dead maps to `unhealthy` since the pipeline cannot process events.

**Alternatives considered:**
- *Defer all pipeline observability to the production metrics stack:* Consistent with the design's treatment of Prometheus/OpenTelemetry as production concerns. But dependency connectivity and pipeline liveness are different categories — connectivity checks verify external systems are reachable, pipeline checks verify the application's own processing loop is functioning. The health endpoint already exists and has a classification model; extending it costs less than building a separate observability path. Rejected — the gap between "three TCP checks" and "full metrics stack" is too wide for the primary health signal.
- *Add a separate `/health/pipeline` endpoint:* Keeps the existing health check simple and adds a more detailed pipeline-specific check. But two health endpoints complicates the docker-compose health check configuration and splits the health model across endpoints. The existing healthy/degraded/unhealthy classification already handles multiple concerns (MongoDB vs ES/Redis). Rejected — one health endpoint with a unified classification is cleaner.

**Rationale:** The existing health model classifies by impact: `unhealthy` means core function is unavailable, `degraded` means partial functionality loss. A dead worker is `unhealthy` (events accepted but never processed). A filling queue or non-empty DLQ is `degraded` (pipeline under pressure but still functioning). These map naturally onto the existing classification without adding new concepts. The implementation cost is trivial — three reads from objects already in scope.

---

## 3. Per-item retryable ES failures (429/5xx) don't trigger any backoff

**Issue:** The batch-cycle exponential backoff (section 4, step 7) only triggers on batch-level connection errors. Per-item 429s — which signal ES thread pool saturation or indexing pressure — cause items to be nacked and retried in the next batch cycle with no delay. The retry path is fast: MongoDB dedup succeeds instantly (cheap unique-index lookup), so nacked items hit ES again almost immediately. Under sustained ES pressure, items cycle through the queue rapidly, burn through their 5-retry budget without giving ES meaningful recovery time, and land in the DLQ. The data isn't lost (MongoDB has it), but the items are permanently missing from the search index — exactly the outcome the retry mechanism was supposed to prevent.

**Decision:** Document as a known limitation of the in-memory queue implementation. Under sustained ES pressure, per-item retryable failures burn through retry budgets without backoff, potentially routing items to the DLQ prematurely. The source of truth (MongoDB) is intact — the impact is items missing from the search index. SQS visibility timeout handles retry timing natively: `nack` maps to `change_message_visibility(0)`, and SQS controls redelivery timing independent of the worker's processing speed. The in-memory queue has no equivalent mechanism, and adding one (e.g., threshold-based backoff triggering on per-item failure rates) introduces a concept with no SQS analogue that complicates the worker for a scenario specific to the in-memory implementation.

**Alternatives considered:**
- *Track per-item retryable failure rate and trigger batch-level backoff above a threshold:* After processing each batch's ES results, if >50% of items failed with 429/5xx, treat it as a batch-level signal and apply the existing exponential backoff. Reuses the consecutive-failure counter. Addresses the symptom, but adds a mechanism (failure-rate threshold driving batch-cycle timing) that doesn't exist in SQS and won't carry over. The in-memory queue already has several documented limitations that SQS eliminates — this is consistent with that pattern. Rejected — over-engineering for a transitional implementation.
- *Increase the default max retry count:* Raise from 5 to a higher number (e.g., 15) to give more attempts under pressure. Crude but simple. Doesn't address the root cause (no delay between retries) — items still hit ES in rapid succession. More retries at the same speed just means more wasted round-trips before the same DLQ outcome. Rejected — treats the symptom poorly.

**Rationale:** This is a suboptimal retry behavior, not a correctness bug. The nack-contention issue (v4 review, issue 1) warranted a fix because it was a deadlock — a correctness problem. Here, the degradation mode is safe: MongoDB has the events, and the DLQ records the ES failures for investigation. The right fix is SQS, which the design already plans for. Documenting the limitation alongside the existing in-memory queue trade-offs (no persistence, no at-least-once, nack contention) is the proportionate response.

---

## 4. ES index and mapping creation is not part of the startup sequence

**Issue:** The design specifies MongoDB index creation at startup (idempotent `create_index`) and defines the ES mapping in detail (section 3), but never says when or how that mapping is applied. If the worker's first bulk write hits ES before the index exists, ES auto-creates it with dynamic mapping — the explicit mapping design (flattened metadata, separate metadata_text, keyword types) would be silently overridden. Dynamic mapping for the metadata field specifically causes the mapping explosion risk noted in the research: individual field mappings per key instead of a single flattened field.

**Decision:** Add ES index creation with the explicit mapping to the lifespan startup sequence, alongside MongoDB client initialization and index creation. ES index creation with `ignore=400` (index already exists) is idempotent, matching the MongoDB `create_index` pattern. This belongs in `main.py` lifespan or `core/database.py` initialization — the same place MongoDB indexes are created.

**Alternatives considered:**
- *Let the worker create the index before its first write:* The worker checks for the index on its first batch and creates it if missing. Keeps startup simple but couples index management to the worker — if the worker hasn't processed any events yet, the index doesn't exist, and search queries against it fail. The lifespan startup is the standard place for infrastructure setup. Rejected — infrastructure should be ready before traffic arrives, not lazily initialized.
- *Use an ES index template instead of explicit creation:* Define a template that matches the index name pattern. ES applies the template's mapping when the index is auto-created by the first write. Works, but adds an indirection (template → index) that's unnecessary for a single index. Index templates are designed for the data streams / time-based rotation pattern that the design defers to production. Rejected — appropriate for the production ILM setup, not for the current single-index scope.

**Rationale:** MongoDB index creation at startup is already in the design. ES index creation is the same pattern — idempotent, runs once at startup, ensures the infrastructure is ready before the worker processes events. The asymmetry (MongoDB indexes planned, ES index not) was an oversight, not a design choice.

---

## 5. No `idempotency_key` filter on GET /events — deliberate omission

**Issue:** `POST /events` returns 202 with `EventAccepted`, and the client's only handle is the `idempotency_key` they provided. But `GET /events` filters by event_type, date range, user_id, and source_url — not `idempotency_key`. A client cannot verify whether their specific event was persisted. The unique index already exists in MongoDB, so adding this filter would be a covered index lookup — essentially free.

**Decision:** Note as a deliberate omission, not a gap. The system's consumer model is fire-and-forget: producers send events and move on, consumers (analytics, search) query by business dimensions, not by ingestion-level identifiers. A producer that needs to retry simply re-sends with the same `idempotency_key` — the dedup logic handles it correctly regardless of whether the first attempt succeeded. Adding an `idempotency_key` filter to `GET /events` serves no current consumer. If a use case arises (e.g., a producer dashboard showing ingestion status), the unique index already supports it — the change is adding one optional query parameter and one filter clause.

**Alternatives considered:**
- *Add the filter now:* The implementation cost is trivial — one optional field on `EventFilter`, one clause in the MongoDB query builder. But the design has been disciplined about not adding features without a justified consumer. An unused filter is still API surface area that must be documented and tested. Rejected — no current use case.
- *Add a dedicated `GET /events/{idempotency_key}` endpoint:* A lookup-by-key endpoint rather than a filter on the list endpoint. Cleaner REST semantics for single-event retrieval. But this goes further than the filter approach — it's a new endpoint, not a parameter on an existing one. Even less justified without a consumer. Rejected — more API surface for the same absent use case.

**Rationale:** The decision to omit is based on the consumer model, not on cost. If the consumer model changes (e.g., producers need confirmation), the implementation path is clear and cheap: add `idempotency_key` as an optional filter on `GET /events`, backed by the existing unique index.
