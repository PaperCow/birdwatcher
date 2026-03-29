# Design Review — 2026-03-29-event-platform-design-v4.md

Reviewed 2026-03-29. Five issues identified, all resolved.

---

## 1. Nack re-enqueue can deadlock or lose messages when the bounded queue is full

**Issue:** The main queue is bounded and the worker is the only consumer. When the worker nacks a message, it re-enqueues it. During the time the worker was processing a batch, new POSTs may have filled the freed slots. If the queue is full at nack time, blocking put (`await queue.put()`) deadlocks — the worker stalls waiting for space, but nothing drains the queue while it's blocked. Non-blocking put (`put_nowait()`) raises `QueueFull`, and without explicit handling the message is silently lost. This scenario is correlated: high load (queue full) and downstream failures (items need retry) tend to happen together.

**Decision:** Nack uses non-blocking `put_nowait()`. On `QueueFull`, the item is routed to the DLQ rather than dropped — the event's MongoDB write has likely already succeeded (source of truth intact), and the DLQ preserves the failure for investigation. This is consistent with the existing DLQ-overflow pattern (DLQ full -> log and drop). The degradation chain is: retry fails to re-enqueue -> DLQ; DLQ full -> log and drop. SQS eliminates this contention entirely — `nack` maps to `change_message_visibility(0)` on a queue with independent capacity, no competition with new enqueues.

**Alternatives considered:**
- *Reserve capacity in the queue for retries:* Set the effective enqueue limit for POSTs lower than the actual queue maxsize (e.g., queue maxsize=1200, POST returns 503 at 1000), reserving remaining slots for nacks. Guarantees retry space under moderate load, but the boundary is arbitrary and can still be exhausted under extreme conditions. Adds a configuration concept (two thresholds for one queue) that doesn't carry over to SQS. Rejected — complexity for an incomplete guarantee.
- *Separate retry queue:* Nacked items go into a second bounded queue. The worker drains from both (retry queue first, then main), giving retry priority. Guarantees retries aren't blocked by new traffic. Adds a component and complicates the drain logic — the worker now has two sources, and the priority-drain ordering is a new concern. Rejected — adds a mechanism that doesn't exist in SQS and complicates the protocol abstraction.
- *Document the gap, don't solve it:* State that under full-queue + downstream-failure conditions, nacked items may be lost. The in-memory queue already loses everything on crash, so this is a smaller version of the same limitation. But the blocking-put variant is a deadlock, not just data loss — it's a correctness bug, not a documented trade-off. Rejected — at minimum the design must specify non-blocking put, and routing to DLQ on failure costs nothing extra.

**Rationale:** The DLQ fallback requires no new mechanisms — it reuses the existing DLQ path and is consistent with how the design handles DLQ overflow. The deadlock risk is eliminated by specifying non-blocking put. SQS eliminates the contention entirely since nack operates on the queue's own capacity, not in competition with producers.

---

## 2. No validation constraints on the metadata field

**Issue:** `EventCreate` accepts an optional `metadata` dict with no size, depth, or key-count limits. This user-provided field flows through three systems: MongoDB (stored as-is — large documents inflate storage and aggregation memory), ES `flattened` field (deeply nested keys become long dotted paths, contributing to `depth_limit` and `total_fields.limit`), and ES `metadata_text` (recursive leaf extraction produces a text string proportional to input size). A pathological payload — very large, deeply nested, or high key-count — could cause ES bulk write rejections for specific items, disproportionate index bloat, or expensive leaf extraction. Metadata is an external input boundary where defensive validation is appropriate.

**Decision:** Add Pydantic validators on `EventCreate.metadata` for maximum serialized size (e.g., 64KB) and maximum nesting depth (e.g., 10 levels). These are defensive limits, not business logic — they prevent pathological inputs from causing disproportionate downstream effects without restricting legitimate use. Validation at the API boundary fails fast with a clear 422 before the event enters the async pipeline.

**Alternatives considered:**
- *Validate in the worker before the ES write, not at the API boundary:* The worker checks metadata size/depth before building the ES document. Keeps the API schema simple, but the event is already accepted (202) and persisted in MongoDB — the validation only prevents ES indexing problems, not MongoDB bloat. The failure surfaces as a DLQ entry rather than a clear client error. Rejected — fails late instead of early, and doesn't protect MongoDB.
- *Rely on ES's built-in limits and handle rejections:* ES will reject documents exceeding its mapping limits. The worker already classifies ES 4xx as permanent failures and routes to DLQ. No new code needed. But the failure mode is opaque (ES rejection rather than a clear validation error), oversized metadata still lands in MongoDB, and systematic bad metadata from a client generates DLQ noise. Rejected — the existing error handling catches it, but as a symptom rather than a cause.
- *Document the gap, don't validate:* Note that metadata is unbounded and that production would add validation. Unlike other deferred-to-production items (SQS, CDC, ILM), this one is cheap to fix now, affects correctness at any scale, and is standard practice for external input boundaries. Rejected.

**Rationale:** Metadata is the one field in the schema that accepts arbitrary user input with no structural constraints. Every other field has type and format validation via Pydantic. Defensive limits on size and depth are consistent with the schema's existing validation approach and cost a few lines in the model definition.

---

## 3. Offset-based pagination is a known scalability limitation

**Issue:** `EventFilter` uses `skip`/`limit` for `GET /events`, and `SearchQuery` uses pagination (presumably `from`/`size`) for `GET /events/search`. Both have known performance cliffs: MongoDB `skip(N)` scans and discards N documents before returning results, and ES `from`/`size` has a hard default limit of 10,000 hits (`index.max_result_window`). For a high-volume event system, these limits are reachable in normal use — filtering events by a broad event_type over a day could easily exceed 10,000 results.

**Decision:** Keep offset-based pagination in the implementation. Document in ARCHITECTURE.md (scaling section) as a known scope simplification: production would use keyset/cursor-based pagination — `_id` or `(timestamp, _id)` as a cursor for MongoDB, and `search_after` for ES. This is consistent with how the design handles other scaling simplifications (single ES index, in-memory queue, standard collections).

**Alternatives considered:**
- *Implement cursor-based pagination now:* Replace `skip` with a `cursor` parameter, use `search_after` for ES. More correct, but changes the API contract (cursor pagination has a different client interaction model — forward-only, no "jump to page N"), adds complexity to filter/sort logic, and the effort-to-signal ratio is low for a portfolio project. The design's strength is in areas where it goes deep (queue protocol, batch error handling, dual-write consistency), not pagination plumbing. Rejected — meaningful effort for a concern that only manifests at volumes well beyond the project's scope.
- *Implement both offset and cursor:* Offer `skip`/`limit` for convenience and a `cursor` parameter as an alternative. Most flexible, but two pagination strategies in one API adds surface area and testing burden. Rejected — over-engineered.

**Rationale:** The design already defers several scaling concerns to ARCHITECTURE.md with clear documentation of the limitation and the production solution. Pagination fits that established pattern. A reviewer is unlikely to flag offset pagination as a problem in a portfolio project — they'd flag the absence of any pagination, or the absence of awareness that offset pagination doesn't scale.

---

## 4. Testing philosophy and "with more time" priorities missing

**Issue:** The requirements specify: "Include a note on testing philosophy and what would be prioritized with more time." Section 7 covers what's tested and how (unit tests, integration tests, test infrastructure) but does not include this note. It is a stated deliverable expectation.

**Decision:** Add a subsection to section 7 covering: the testing philosophy (why testcontainers over mocks for MongoDB/ES, why fakeredis is sufficient for Redis, what the unit/integration split optimizes for) and "with more time" priorities (load/performance testing of batch throughput, chaos testing of failure paths, contract testing for the ES mapping, fuzz testing of metadata validation).

**Alternatives considered:**
- *Defer to implementation — write it directly in the README:* The note is prose, not architecture. Skip planning it and write it when writing the README. Risk: it gets forgotten during implementation, which is how it ended up missing from the design in the first place. Rejected.
- *Fold it into the ARCHITECTURE.md plan:* The ARCHITECTURE.md has a "what would change in production" section where testing priorities could live. But the requirements frame it as part of the testing deliverable, not the architecture doc. Rejected — put it where the requirements expect it.

**Rationale:** Writing the philosophy note in the design forces articulation of the reasoning behind the test infrastructure choices, which strengthens the design. The content is mostly already implicit in section 7 — the choices are made, the rationale just isn't stated.

---

## 5. README.md is a required deliverable but isn't planned

**Issue:** The requirements list four deliverables: source code, ARCHITECTURE.md, tests, and README.md ("with setup instructions, endpoint documentation, and testing approach"). Section 9 plans ARCHITECTURE.md in detail — six sections with mapped content. README.md is not mentioned anywhere in the design.

**Decision:** Add a lightweight section to the design doc planning the README structure: setup/run instructions (docker-compose, env vars, prerequisites), endpoint reference (each route with request/response examples), and how to run tests (pytest markers for unit vs integration, testcontainers prerequisites). Does not need the depth of the ARCHITECTURE.md plan — just enough to ensure the deliverable isn't forgotten during implementation.

**Alternatives considered:**
- *Don't plan it — write it during implementation:* A README is standard enough that it doesn't need design-level planning. The content follows directly from implementation decisions already made. Risk of forgetting is low since it's a visible, familiar deliverable. But the ARCHITECTURE.md was also "standard" and got a full planning section — the asymmetry is notable. Rejected — a few bullet points costs nothing and ensures coverage.
- *Merge into the ARCHITECTURE.md plan as a note:* Add a line in section 9 acknowledging it. Acknowledges the deliverable without a separate section. But the README and ARCHITECTURE.md are different documents with different audiences (README is for setup/usage, ARCHITECTURE.md is for understanding the system). Rejected — conflates two deliverables.

**Rationale:** The value is ensuring the deliverable doesn't fall through the cracks, not planning its content in detail. A lightweight section is proportionate to the risk.
