# Requirements

Locked requirements from `idea.md`. Do not re-evaluate during design or implementation.

## Tech Stack

- Python with FastAPI
- MongoDB for event storage and aggregations
- Elasticsearch for full-text search and analytics queries
- Redis for caching
- In-process simulated event queue (SQS-style)

## Endpoints

- `POST /events` — Validate and enqueue events for async processing
- `GET /events` — Filter by type, date range, user ID, source URL
- `GET /events/stats` — Aggregation counts by event type and time bucket (hourly, daily, weekly)
- `GET /events/search` — Full-text search across event metadata via Elasticsearch
- `GET /events/stats/realtime` — Stats summary from Redis cache with configurable TTL

## Event Schema

- Event type (`string`), timestamp (`datetime`), user ID (`string`), source URL (`string`), metadata (`object`)

## Ingestion Pipeline

- Async: enqueue on POST, background worker writes to MongoDB
- Retry failed events with basic backoff
- Document queue guarantees and SQS comparison

## Caching

- Redis caching required for `/events/stats/realtime`
- Document TTL rationale, invalidation approach, and high-write-volume considerations

## Indexing

- MongoDB indexes for query patterns, with documented reasoning
- Elasticsearch index mapping with field type and analyzer rationale
- Document indexes deliberately omitted and why

## Architecture Document (`ARCHITECTURE.md`)

- System diagram (data flow: ingestion → storage → query)
- Component responsibilities for each layer
- Storage rationale (why MongoDB vs Elasticsearch vs Redis for each concern)
- Failure modes and graceful degradation
- Scaling considerations (what breaks at 10x volume)
- What would change with more time or in production

## Testing

- Unit tests for core logic and error paths
- Integration tests for at least two full request lifecycles
- pytest preferred
- Note on testing philosophy and priorities

## Code Quality

- Clear module boundaries: ingestion, processing, storage, querying, caching
- Meaningful error handling with proper HTTP responses and logging
- Maintainable by a team

## Deliverables

- Source code with clear module structure
- `ARCHITECTURE.md` standalone document
- Unit and integration tests
- `README.md` with setup, endpoint docs, and testing approach

## Stretch Goals (optional)

- Docker/docker-compose for all services
- Rate limiting / abuse prevention middleware
- Event deduplication in worker
- Dead letter queue simulation
- SQS drop-in design notes

## Scope

- Backend only. No frontend, UI, or styling.
