# Change Stream Migration Guide

This document describes how the current dual-write approach differs from MongoDB
change streams for syncing data to Elasticsearch, what each approach guarantees,
and a step-by-step guide for migrating.

The change stream consumer runs as a **separate service** from the API — same
Docker image, different entrypoint. This gives independent scaling, deployment,
and failure isolation between the write path (API + worker) and the ES sync path
(consumer).

## How Dual Write Differs from Change Streams

| Aspect | Dual Write (current) | Change Streams |
|---|---|---|
| **Coupling** | Worker writes to both stores in sequence | MongoDB write and ES indexing are independent services |
| **ES indexing latency** | Same batch cycle as MongoDB write (~5s max) | Eventual — sub-second typical, higher under load or elections |
| **Consistency model** | ES indexed when batch completes (tight coupling) | Eventually consistent — MongoDB always ahead of ES |
| **ES retry cost** | Full queue retry: re-inserts into MongoDB (hits unique index), then retries ES | Change stream consumer retries ES only — MongoDB already has the event |
| **Failure blast radius** | ES outage causes queue nacks, backpressure, and DLQ routing | ES outage only affects the consumer; write path and queue are unaffected |
| **Ordering** | Concurrent batches can race on ES writes | Oplog order — strictly ordered by MongoDB write sequence |
| **MongoDB requirement** | Standalone or replica set | Replica set required (needs oplog) |
| **Recovery after crash** | Lost in-flight messages (in-memory queue) | Resume token — consumer restarts from last checkpoint |
| **Data retention sync** | Separate ILM policy for ES, must coordinate with MongoDB TTL | MongoDB TTL deletes propagate as delete events through the change stream |
| **Worker complexity** | Per-item error classification across two stores | Worker is MongoDB-only; consumer handles ES errors independently |
| **Scaling** | Single process, throughput bound by slower store | API and consumer scale independently |
| **Deployment** | Single deployment | Two deployments from same image — API fix doesn't bounce consumer |

## What the Dual Write Guarantees

- **Atomic batch outcome**: Each batch produces a clear result — items are acked, nacked, or DLQ'd
- **ES indexed on ack**: When a message is acked, it exists in both MongoDB and ES
- **Per-item error routing**: Retryable ES failures (429/5xx) are nacked; permanent failures (4xx) are acked and logged
- **Single process**: No additional services to deploy or monitor

## What It Does NOT Guarantee

- **Independent failure handling**: An ES outage creates backpressure on the entire write path, even though MongoDB writes succeed
- **Efficient retries**: ES retry re-inserts into MongoDB (redundant unique-index lookup on every retry)
- **Ordering**: Concurrent batches can write to ES out of order
- **Decoupled scaling**: Worker throughput is bound by the slower store

## Why a Separate Service, Not a Background Task

Running the consumer as an `asyncio.create_task` inside the API process is simpler
but creates operational problems:

- **Shared failure domain.** An API crash or deploy restarts the consumer too,
  forcing it to re-seek from its last checkpoint. A consumer bug (e.g., unhandled
  cursor invalidation) takes down the API.
- **Competing for resources.** The consumer holds a long-lived MongoDB cursor and
  does bulk ES writes. In the same event loop as the API, a slow ES bulk call can
  delay request handling.
- **Coupled scaling.** The API scales on request volume. The consumer scales on
  oplog throughput. These are uncorrelated — a burst of search queries shouldn't
  compete with change stream processing.
- **Coupled deploys.** An API-only fix shouldn't bounce the consumer and lose its
  cursor position, and vice versa.

A separate repo would be overkill — the consumer shares models, config, database
setup, and ES bulk indexing logic. Duplicating across repos creates drift. Instead,
the same repo produces one Docker image with two entrypoints.

## Migration Steps

### 1. Configure MongoDB as a Replica Set

Change streams require an oplog, which only exists on replica sets.

Update `docker-compose.yml` — add `--replSet` and an init service:

```yaml
mongodb:
  image: mongo:8
  ports:
    - "27017:27017"
  volumes:
    - mongodb_data:/data/db
  command: ["--replSet", "rs0"]
  healthcheck:
    test: ["CMD", "mongosh", "--eval", "try { rs.status().ok } catch(e) { rs.initiate().ok }"]
    interval: 10s
    timeout: 5s
    retries: 5
    start_period: 10s
```

The healthcheck doubles as the replica set initiator — on first boot `rs.status()` fails, so
it calls `rs.initiate()`. On subsequent checks `rs.status()` succeeds directly.

Update `APP_MONGODB_URL` in the app service to include the replica set name:

```yaml
- APP_MONGODB_URL=mongodb://mongodb:27017/?replicaSet=rs0&directConnection=true
```

`directConnection=true` is needed for single-node replica sets where the driver
cannot discover other members via the topology.

### 2. Add Configuration

Add to `src/config.py`:

```python
# Service role — which entrypoint this process runs
# "api" = FastAPI + worker (default)
# "consumer" = change stream consumer only
role: str = "api"

# Change stream consumer (set sync_backend="change_stream" to activate)
sync_backend: str = "dual_write"
change_stream_batch_size: int = 100
change_stream_batch_timeout: float = 2.0
change_stream_checkpoint_interval: int = 100  # persist resume token every N documents
```

The `role` field controls which entrypoint runs. Both roles read the same config;
the consumer simply ignores API-specific settings (rate limiting, queue size, etc.)
and the API ignores consumer-specific settings.

### 3. Create the Change Stream Consumer

Create `src/sync/consumer.py` implementing the ES sync via change streams.

Core responsibilities:
- Open a `watch()` cursor on the `events` collection, filtered to `insert` and `delete` operations
- Batch incoming change events by count (`change_stream_batch_size`) or time (`change_stream_batch_timeout`)
- Transform each change document into an ES bulk action (same shape as the current `_to_es_action` in `worker.py`)
- Bulk-index into ES via `async_bulk`
- Persist a resume token to a MongoDB collection at a configurable interval

```python
class ChangeStreamConsumer:
    def __init__(
        self,
        events_collection,
        checkpoints_collection,
        es_client,
        es_index: str,
        batch_size: int,
        batch_timeout: float,
        checkpoint_interval: int,
        shutdown_event: asyncio.Event,
    ):
        ...

    async def run(self) -> None:
        resume_token = await self._load_resume_token()
        pipeline = [{"$match": {"operationType": {"$in": ["insert", "delete"]}}}]
        async with self._events.watch(
            pipeline,
            resume_after=resume_token,
            full_document="updateLookup",
        ) as stream:
            ...

    async def _load_resume_token(self) -> dict | None:
        doc = await self._checkpoints.find_one({"_id": "change_stream"})
        return doc["resume_token"] if doc else None

    async def _save_resume_token(self, token: dict) -> None:
        await self._checkpoints.update_one(
            {"_id": "change_stream"},
            {"$set": {"resume_token": token, "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
```

**Resume token storage**: A `change_stream_checkpoints` collection in the same MongoDB
database. The consumer persists the token every `checkpoint_interval` documents. On
restart, it resumes from the last checkpoint — events between the checkpoint and the
crash are re-delivered, but ES writes are idempotent (`_id` = `idempotency_key`), so
duplicates overwrite harmlessly.

**Delete propagation**: When a MongoDB TTL index deletes a document, the change stream
emits a `delete` event. The consumer translates this into an ES `delete` action, keeping
retention in sync automatically without a separate ILM policy.

### 4. Create the Checkpoint Collection

Add to `DatabaseManager.create_indexes()` in `src/core/database.py`:

```python
# Change stream checkpoint (only needed when sync_backend="change_stream")
if self.settings.sync_backend == "change_stream":
    await self.mongo_db.create_collection(
        "change_stream_checkpoints",
    )
```

`create_collection` is a no-op if the collection already exists (with the appropriate
`ignore` on the error). This keeps it consistent with the existing idempotent index
creation pattern.

### 5. Simplify the Worker

When `sync_backend="change_stream"`, remove all ES logic from `EventWorker` in
`src/ingestion/worker.py`:

- Remove `_write_elasticsearch()` (lines 119-153)
- Remove `_to_es_action()` (lines 167-179)
- Remove the `es_client` and `es_index` constructor parameters
- Remove the ES conditional in `process_batch()` (lines 92-94)

`process_batch` becomes:

```python
async def process_batch(self, batch: list[ReceivedMessage]) -> None:
    succeeded, failed = await self._write_mongodb(batch)

    for received, error_msg in failed:
        logger.warning("mongodb_item_failed", receipt_handle=received.receipt_handle, error=error_msg)
        await self._queue.nack(received.receipt_handle)

    for received in succeeded:
        await self._queue.ack(received.receipt_handle)
```

The ack/nack decision is now based solely on MongoDB success. ES indexing is the change
stream consumer's responsibility.

### 6. Create the Consumer Entrypoint

Create `src/consumer_main.py` — a standalone async runner with no FastAPI or HTTP
server. This is what the consumer Kubernetes deployment runs.

```python
# src/consumer_main.py
from __future__ import annotations

import asyncio
import signal

from src.config import get_settings
from src.core.database import DatabaseManager
from src.core.logging import configure_logging, get_logger
from src.sync.consumer import ChangeStreamConsumer

logger = get_logger(component="consumer_main")


async def main() -> None:
    configure_logging()
    settings = get_settings()

    db = DatabaseManager(settings)
    await db.connect()
    await db.create_indexes()

    shutdown_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    consumer = ChangeStreamConsumer(
        events_collection=db.mongo_db["events"],
        checkpoints_collection=db.mongo_db["change_stream_checkpoints"],
        es_client=db.es_client,
        es_index=settings.elasticsearch_index,
        batch_size=settings.change_stream_batch_size,
        batch_timeout=settings.change_stream_batch_timeout,
        checkpoint_interval=settings.change_stream_checkpoint_interval,
        shutdown_event=shutdown_event,
    )

    logger.info("consumer_starting")
    try:
        await consumer.run()
    finally:
        await db.close()
        logger.info("consumer_shutdown_complete")


if __name__ == "__main__":
    asyncio.run(main())
```

The consumer process:
- Connects to MongoDB and Elasticsearch (same `DatabaseManager`)
- Listens for `SIGTERM`/`SIGINT` for graceful shutdown (Kubernetes sends `SIGTERM` on pod termination)
- Runs the change stream loop until shutdown
- Has no HTTP server, no queue, no rate limiting — just the change stream cursor and ES bulk writes

### 7. Update main.py

The API entrypoint changes are minimal — just strip ES from the worker when using
change streams. The consumer is no longer started here.

In the `lifespan` function:

```python
# Worker (MongoDB-only when using change streams)
if settings.sync_backend == "change_stream":
    worker = EventWorker(
        queue=queue,
        mongo_collection=db.mongo_db["events"],
        batch_size=settings.batch_size,
        batch_timeout=settings.batch_timeout,
        backoff_base=settings.backoff_base,
        backoff_max=settings.backoff_max,
        shutdown_event=shutdown_event,
    )
else:
    worker = EventWorker(
        queue=queue,
        mongo_collection=db.mongo_db["events"],
        es_client=db.es_client,
        es_index=settings.elasticsearch_index,
        batch_size=settings.batch_size,
        batch_timeout=settings.batch_timeout,
        backoff_base=settings.backoff_base,
        backoff_max=settings.backoff_max,
        shutdown_event=shutdown_event,
    )

worker_task = asyncio.create_task(worker.run())
```

Shutdown remains unchanged — only the worker task to await.

### 8. Add Health Checks

Each service gets its own health endpoint.

**API health** (`src/health/router.py`) — unchanged. The API no longer knows about
the consumer, so it reports on its own components only: worker, queue, backends.

**Consumer health** — the consumer needs a lightweight HTTP health endpoint for
Kubernetes liveness and readiness probes. Add a minimal health server inside the
consumer process:

```python
# In consumer_main.py, after starting the consumer
from aiohttp import web

async def health_handler(request: web.Request) -> web.Response:
    alive = consumer.is_alive()
    lag = consumer.lag_seconds()
    status = 200 if alive else 503
    return web.json_response(
        {"alive": alive, "lag_seconds": lag},
        status=status,
    )

health_app = web.Application()
health_app.router.add_get("/health", health_handler)
runner = web.AppRunner(health_app)
await runner.setup()
site = web.TCPSite(runner, "0.0.0.0", 8001)
await site.start()
```

Alternatively, use a file-based liveness probe to avoid adding a dependency — the
consumer writes a heartbeat timestamp to a file, and the Kubernetes liveness probe
checks its age:

```yaml
livenessProbe:
  exec:
    command: ["python", "-c", "import os,time; assert time.time()-os.path.getmtime('/tmp/heartbeat')<30"]
  periodSeconds: 10
```

The HTTP approach is more informative (exposes lag metrics to monitoring), but the
file approach has zero additional dependencies. Choose based on whether you want
to add `aiohttp` (or a similarly lightweight HTTP library) to the project.

The consumer should expose:
- `is_alive()` — heartbeat-based liveness, same pattern as the worker
- `lag_seconds()` — difference between the cluster's latest oplog timestamp and the
  consumer's last processed timestamp (requires periodic reads of the oplog `ts` field)

### 9. Handle Oplog Sizing

The oplog is a capped collection with finite retention. If the change stream consumer
falls behind by more than the oplog window (e.g., prolonged ES outage), it loses its
place and cannot resume — the resume token becomes invalid.

Mitigations:
- **Monitor consumer lag** via the health endpoint. Alert when lag exceeds 50% of the
  oplog window.
- **Size the oplog appropriately.** Default is 5% of disk. For high-throughput workloads,
  explicitly set `oplogSizeMB` in the MongoDB configuration to ensure sufficient
  retention (at minimum, enough to cover the longest expected ES outage).
- **Reindex fallback.** If the resume token is invalidated, the consumer must do a full
  reindex from MongoDB to ES, then resume from the current oplog position. This should
  be an explicit manual operation, not automatic — a full reindex under load can saturate
  ES.

### 10. Update docker-compose.yml

Add a second service for the consumer using the same image:

```yaml
consumer:
  build: .
  command: [".venv/bin/python", "-m", "src.consumer_main"]
  environment:
    - APP_ROLE=consumer
    - APP_SYNC_BACKEND=change_stream
    - APP_MONGODB_URL=mongodb://mongodb:27017/?replicaSet=rs0&directConnection=true
    - APP_ELASTICSEARCH_URL=http://elasticsearch:9200
    - APP_REDIS_URL=redis://redis:6379
  depends_on:
    mongodb:
      condition: service_healthy
    elasticsearch:
      condition: service_healthy

app:
  build: .
  ports:
    - "8000:8000"
  environment:
    - APP_SYNC_BACKEND=change_stream
    - APP_MONGODB_URL=mongodb://mongodb:27017/?replicaSet=rs0&directConnection=true
    - APP_ELASTICSEARCH_URL=http://elasticsearch:9200
    - APP_REDIS_URL=redis://redis:6379
  depends_on:
    mongodb:
      condition: service_healthy
    elasticsearch:
      condition: service_healthy
    redis:
      condition: service_healthy
```

Both services use the same `build: .` — same image, different `command`. The consumer
does not need Redis (no rate limiting, no caching), so it omits that dependency.

### 11. Kubernetes Deployment

The Dockerfile requires no changes. Both deployments use the same image with different
`command` overrides.

**API deployment** (scales on request traffic):

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: birdwatcher-api
spec:
  replicas: 3
  selector:
    matchLabels:
      app: birdwatcher
      component: api
  template:
    metadata:
      labels:
        app: birdwatcher
        component: api
    spec:
      containers:
      - name: api
        image: birdwatcher:latest
        command: [".venv/bin/uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
        ports:
        - containerPort: 8000
        env:
        - name: APP_SYNC_BACKEND
          value: change_stream
        readinessProbe:
          httpGet:
            path: /health
            port: 8000
          periodSeconds: 10
        resources:
          requests:
            cpu: 500m
            memory: 512Mi
```

**Consumer deployment** (scales on oplog throughput):

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: birdwatcher-consumer
spec:
  replicas: 1
  selector:
    matchLabels:
      app: birdwatcher
      component: consumer
  template:
    metadata:
      labels:
        app: birdwatcher
        component: consumer
    spec:
      containers:
      - name: consumer
        image: birdwatcher:latest
        command: [".venv/bin/python", "-m", "src.consumer_main"]
        env:
        - name: APP_ROLE
          value: consumer
        - name: APP_SYNC_BACKEND
          value: change_stream
        livenessProbe:
          exec:
            command: ["python", "-c", "import os,time; assert time.time()-os.path.getmtime('/tmp/heartbeat')<30"]
          periodSeconds: 10
        resources:
          requests:
            cpu: 250m
            memory: 256Mi
```

**Why a Deployment, not a CronJob:** A change stream is a persistent tailable cursor
on the oplog — it's designed to stay open indefinitely and receive events in real time.
A CronJob would mean repeatedly opening/closing the cursor, introducing latency gaps
and unnecessary checkpoint overhead. The resume token mechanism is designed for crash
recovery, not deliberate stop/start cycles.

A CronJob **is** appropriate for the one-time backfill operation (step 14).

### 12. Horizontal Scaling

A single MongoDB change stream on a single collection produces one ordered sequence
of events. Unlike a partitioned queue (SQS, Kafka), it cannot be trivially split
across multiple consumers.

**Start with one replica.** A single async Python process doing bulk ES writes can
handle significant throughput — bulk indexing 1000+ docs/sec to ES is within reach.
Tune `change_stream_batch_size` and `change_stream_batch_timeout` for throughput.

**When to scale:** Monitor these metrics via the consumer health endpoint:
- **Consumer lag** (oplog timestamp - last processed timestamp): if this consistently
  grows under normal load, the consumer is falling behind
- **ES bulk indexing latency** (p99 of bulk call duration): usually the bottleneck
- **Checkpoint frequency**: if checkpoints slow down, too much time is spent in ES

**Scaling options, in order of complexity:**

1. **Vertical scaling.** Increase `change_stream_batch_size`, tune ES bulk settings
   (`chunk_size`, `max_retries`), increase container CPU/memory. Easiest, often sufficient.

2. **Application-level fan-out.** One "leader" consumer reads the change stream and
   distributes events to N worker goroutines/tasks via an internal queue. The leader is
   a single reader, but ES bulk work fans out. Good when ES writes are the bottleneck,
   not change stream reads.

3. **Intermediate queue.** Route change stream events into Kafka/SQS partitions (by
   `event_type` or a hash key), then have N independent consumers each processing a
   partition. Adds infrastructure but gives true horizontal scaling with ordering
   guarantees per partition. Aligns with the SQS migration already documented.

4. **Sharded collection.** If the MongoDB `events` collection is sharded, you can open
   one change stream per shard, each handled by a separate consumer instance. Requires
   a sharded MongoDB cluster — significant infrastructure change.

Option 1 handles most workloads. If it hits a ceiling, Option 3 (intermediate queue)
is the most operationally clean path and composes well with the SQS migration.

### 13. Testing

The integration test setup in `conftest.py` needs two changes:

**Replica set testcontainer.** The `MongoDbContainer` starts a standalone instance.
Override the command to start a single-node replica set:

```python
@pytest.fixture(scope="session")
def mongodb_container():
    with MongoDbContainer("mongo:8").with_command(
        "--replSet rs0"
    ) as c:
        # Initiate the replica set
        client = MongoClient(c.get_connection_url(), directConnection=True)
        client.admin.command("replSetInitiate")
        # Wait for the node to become primary
        while True:
            status = client.admin.command("replSetGetStatus")
            if status["members"][0]["stateStr"] == "PRIMARY":
                break
            time.sleep(0.5)
        client.close()
        yield c
```

**Wait strategy.** The current `wait_for_processing` joins the queue and refreshes ES.
With change streams, the queue join only confirms MongoDB writes — ES indexing happens
asynchronously via the consumer. Replace with a poll:

```python
async def wait_for_processing(app):
    """Wait for all queued events to be processed and indexed."""
    await app.state.queue.join()  # MongoDB writes complete

    if app.state.settings.sync_backend == "change_stream":
        # Poll ES until expected document count appears
        es = app.state.db.es_client
        index = app.state.settings.elasticsearch_index
        for _ in range(50):  # 5s max
            await es.indices.refresh(index=index)
            count = (await es.count(index=index))["count"]
            if count > 0:
                break
            await asyncio.sleep(0.1)
    else:
        await app.state.db.es_client.indices.refresh(
            index=app.state.settings.elasticsearch_index
        )
```

**Integration tests for the consumer itself** should test the consumer entrypoint
in isolation — start a `ChangeStreamConsumer` against the testcontainer replica set,
insert documents directly into MongoDB, and assert they appear in ES. This validates
the consumer independently of the API.

### 14. Backfill Existing Data

If migrating a running system that already has events in MongoDB but not yet indexed
via change streams, a one-time backfill is needed. The change stream only captures
new operations from the point the cursor is opened.

Approach:
1. Open the change stream cursor **first** and record the initial resume token
2. Run a full scan of the MongoDB `events` collection and bulk-index all documents into ES
3. Start consuming from the recorded resume token

This ensures no gap between the historical backfill and the live stream. Events that
arrive during the backfill are captured by the change stream and applied after — ES
writes are idempotent, so overlapping documents are harmlessly overwritten.

This is a one-shot operation — a Kubernetes Job or CronJob is appropriate here,
not the long-running consumer deployment.
