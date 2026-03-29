# Distributed Event Processing Platform

## Overview

Design and implement a production-grade system for ingesting, processing, querying, and caching high-volume web events. The system should handle asynchronous event ingestion, persistent storage, full-text search, analytics aggregation, and caching — with clear architectural boundaries between each concern.

An architecture document is a first-class deliverable alongside the code.

## Tech Stack

- **Framework:** Python with FastAPI or Flask
- **Primary store:** MongoDB (event storage and aggregations)
- **Search/analytics layer:** Elasticsearch (full-text search and analytics queries)
- **Caching layer:** Redis
- **Async processing:** Simulated event queue (in-process, modeled after SQS-style processing)

## Core Requirements

### 1. Async Event Ingestion Pipeline

Rather than writing events synchronously to MongoDB on each request, implement an async ingestion pipeline:

- `POST /events` — Accept event data, validate it, and enqueue it for async processing
- A background worker consumes from the queue and writes events to MongoDB
- If processing fails, events should be retried with a basic backoff strategy
- Document the queue design: what guarantees does it provide, and what would change if this were a real SQS implementation?

**Event structure:**

| Field | Type | Description |
|-------|------|-------------|
| Event type | `string` | e.g., `"pageview"`, `"click"`, `"conversion"` |
| Timestamp | datetime | When the event occurred |
| User ID | `string` | Identifier for the user |
| Source URL | `string` | Originating URL |
| Metadata | `object` | Flexible JSON: browser info, device type, feature-specific data |

### 2. Querying & Analytics

- `GET /events` — Filter events by type, date range, user ID, or source URL
- `GET /events/stats` — MongoDB aggregation pipeline returning counts grouped by event type and configurable time bucket (hourly, daily, weekly)
- `GET /events/search` — Full-text search across event metadata using Elasticsearch
- `GET /events/stats/realtime` — Lightweight stats summary served from Redis cache, with a configurable TTL

### 3. Caching Strategy

- Implement Redis caching for the `/events/stats/realtime` endpoint
- Document the caching strategy: TTL rationale, cache invalidation approach, and what would change under higher write volume

### 4. Indexing Strategy

- Implement appropriate MongoDB indexes for query patterns and document the reasoning
- Define an Elasticsearch index mapping for event documents and explain field type and analyzer choices
- Identify any indexes deliberately not added and why

### 5. Architecture Document

Include a dedicated `ARCHITECTURE.md` covering:

- **System diagram** — Text, ASCII, or linked image showing data flow from ingestion to storage to query
- **Component responsibilities** — What each layer (API, queue, worker, MongoDB, Elasticsearch, Redis) owns and why
- **Storage rationale** — Why responsibility is split between MongoDB, Elasticsearch, and Redis the way it is
- **Failure modes** — What happens if MongoDB is unavailable? If the worker crashes mid-batch? How does the system degrade gracefully?
- **Scaling considerations** — If event volume 10x'd, what breaks first and how would you address it?
- **What you'd do differently** — Given more time or a real production environment, what would change and why?

### 6. Testing

- Unit tests covering core business logic and error paths
- Integration tests covering at least two full request lifecycles (e.g., ingest → worker processes → query returns result)
- pytest preferred
- Include a note on testing philosophy and what would be prioritized with more time

### 7. Code Quality & Standards

- Clear module boundaries — ingestion, processing, storage, querying, and caching are distinct concerns
- Meaningful error handling with appropriate HTTP responses and internal logging
- Code should be written as if a team of engineers will maintain it

## Stretch Goals

- Dockerfile / docker-compose setup covering all services (MongoDB, Elasticsearch, Redis, app)
- Basic rate limiting or abuse prevention middleware
- Event deduplication logic in the worker
- Dead letter queue simulation for events that exhaust retries
- AWS SQS drop-in design notes — what would change if the in-process queue were replaced with real SQS?

## Scope

This is strictly a backend system. No frontend, UI components, or styling.

## Deliverables

- Source code with clear module structure
- `ARCHITECTURE.md` as a standalone document
- Unit and integration tests
- `README.md` with setup instructions, endpoint documentation, and testing approach
