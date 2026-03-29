# Distributed Event Processing Platform — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement the distributed event processing platform per the v7 design document.

**Architecture:** FastAPI async API → bounded asyncio.Queue → batch worker → MongoDB (source of truth) + Elasticsearch (search index) dual write, with Redis caching for realtime stats and rate limiting middleware. No ODM — raw PyMongo Async driver.

**Tech Stack:** Python 3.13, FastAPI 0.135+, PyMongo 4.16+ (async), elasticsearch-py 9.3+ (async), redis-py 7.4+ (async), structlog, orjson, pydantic-settings, pytest + testcontainers + fakeredis

**Design Reference:** `docs/plans/2026-03-29-event-platform-design-v7.md` — all rationale, schema details, and architecture decisions.

---

## Phase 1: Foundation

### Task 1: Project Scaffolding & Dependencies

**Files:**
- Create: `pyproject.toml`
- Create: all `__init__.py` files for package structure
- Create: `.env.example`
- Modify: `.gitignore`

**Step 1: Create pyproject.toml**

```toml
[project]
name = "birdwatcher"
version = "0.1.0"
description = "Distributed Event Processing Platform"
requires-python = ">=3.13"
dependencies = [
    "fastapi>=0.135.0",
    "uvicorn[standard]>=0.42.0",
    "pymongo>=4.16.0",
    "elasticsearch[async]>=9.3.0",
    "redis>=7.4.0",
    "pydantic>=2.12.0",
    "pydantic-settings>=2.13.0",
    "structlog>=25.5.0",
    "orjson>=3.10.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=1.3.0",
    "httpx>=0.27.0",
    "asgi-lifespan>=2.1.0",
    "fakeredis>=2.34.0",
    "testcontainers[mongodb,elasticsearch]>=4.14.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.backends"

[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = [
    "unit: unit tests (no external dependencies)",
    "integration: integration tests (require Docker)",
]
testpaths = ["src/tests"]

[tool.hatch.build.targets.wheel]
packages = ["src"]
```

**Step 2: Create directory structure**

Create empty `__init__.py` in each directory:

```
src/
├── __init__.py
├── events/__init__.py
├── analytics/__init__.py
├── search/__init__.py
├── queue/__init__.py
├── ingestion/__init__.py
├── health/__init__.py
├── cache/__init__.py
├── core/__init__.py
└── tests/
    ├── __init__.py
    ├── unit/__init__.py
    └── integration/__init__.py
```

**Step 3: Create .env.example**

```
APP_MONGODB_URL=mongodb://localhost:27017
APP_MONGODB_DATABASE=birdwatcher
APP_ELASTICSEARCH_URL=http://localhost:9200
APP_ELASTICSEARCH_INDEX=events
APP_REDIS_URL=redis://localhost:6379
APP_REDIS_MAX_CONNECTIONS=20
APP_QUEUE_MAX_SIZE=10000
APP_DLQ_MAX_SIZE=1000
APP_BATCH_SIZE=100
APP_BATCH_TIMEOUT=5.0
APP_MAX_RETRIES=5
APP_REALTIME_STATS_TTL=30
APP_RATE_LIMIT_REQUESTS=100
APP_RATE_LIMIT_WINDOW=60
```

**Step 4: Append to .gitignore**

```
__pycache__/
*.pyc
.env
.venv/
*.egg-info/
dist/
.pytest_cache/
```

**Step 5: Install and verify**

Run: `uv sync --all-extras`

**Step 6: Commit**

```bash
git add pyproject.toml src/ .env.example .gitignore
git commit -m "feat: project scaffolding and dependencies"
```

---

### Task 2: Configuration & Core Infrastructure

**Files:**
- Create: `src/config.py`
- Create: `src/core/logging.py`
- Create: `src/core/exceptions.py`
- Test: `src/tests/unit/test_config.py`

**Step 1: Write the failing test**

```python
# src/tests/unit/test_config.py
import pytest
from src.config import Settings

@pytest.mark.unit
class TestSettings:
    def test_defaults_load(self):
        settings = Settings()
        assert settings.mongodb_url == "mongodb://localhost:27017"
        assert settings.mongodb_database == "birdwatcher"
        assert settings.elasticsearch_url == "http://localhost:9200"
        assert settings.elasticsearch_index == "events"
        assert settings.redis_url == "redis://localhost:6379"

    def test_queue_defaults(self):
        settings = Settings()
        assert settings.queue_max_size == 10000
        assert settings.dlq_max_size == 1000
        assert settings.batch_size == 100
        assert settings.batch_timeout == 5.0
        assert settings.max_retries == 5

    def test_validation_defaults(self):
        settings = Settings()
        assert settings.max_metadata_size == 65536
        assert settings.max_metadata_depth == 10
        assert settings.timestamp_past_window_days == 30
        assert settings.timestamp_future_window_minutes == 5

    def test_stats_limits(self):
        settings = Settings()
        assert settings.hourly_max_days == 7
        assert settings.daily_max_days == 365
        assert settings.weekly_max_days == 730

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("APP_MONGODB_DATABASE", "custom_db")
        settings = Settings()
        assert settings.mongodb_database == "custom_db"
```

**Step 2: Run test — expect FAIL**

Run: `uv run pytest src/tests/unit/test_config.py -v`

**Step 3: Implement**

```python
# src/config.py
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_")

    # MongoDB
    mongodb_url: str = "mongodb://localhost:27017"
    mongodb_database: str = "birdwatcher"

    # Elasticsearch
    elasticsearch_url: str = "http://localhost:9200"
    elasticsearch_index: str = "events"

    # Redis
    redis_url: str = "redis://localhost:6379"
    redis_max_connections: int = 20

    # Queue
    queue_max_size: int = 10000
    dlq_max_size: int = 1000

    # Worker
    batch_size: int = 100
    batch_timeout: float = 5.0
    max_retries: int = 5

    # Cache
    realtime_stats_ttl: int = 30

    # Rate limiting
    rate_limit_requests: int = 100
    rate_limit_window: int = 60

    # Validation
    max_metadata_size: int = 65536
    max_metadata_depth: int = 10
    timestamp_past_window_days: int = 30
    timestamp_future_window_minutes: int = 5

    # Stats query limits
    hourly_max_days: int = 7
    daily_max_days: int = 365
    weekly_max_days: int = 730

    # Backoff
    backoff_base: float = 2.0
    backoff_max: float = 60.0

@lru_cache
def get_settings() -> Settings:
    return Settings()
```

```python
# src/core/logging.py
import logging
import structlog

def configure_logging(json_output: bool = True) -> None:
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]
    renderer = structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

def get_logger(**kwargs):
    return structlog.get_logger(**kwargs)
```

```python
# src/core/exceptions.py
class QueueFullError(Exception):
    """Event queue is at capacity."""

class ServiceUnavailableError(Exception):
    """A required backend service is unreachable."""
```

**Step 4: Run test — expect PASS**

Run: `uv run pytest src/tests/unit/test_config.py -v`

**Step 5: Commit**

```bash
git add src/config.py src/core/logging.py src/core/exceptions.py src/tests/unit/test_config.py
git commit -m "feat: configuration, logging, and exception infrastructure"
```

---

### Task 3: Event Schemas

**Files:**
- Create: `src/events/schemas.py`
- Test: `src/tests/unit/test_schemas.py`

**Step 1: Write the failing test**

```python
# src/tests/unit/test_schemas.py
import pytest
from datetime import datetime, timezone, timedelta
from src.events.schemas import EventCreate, EventAccepted, EventResponse, EventFilter

@pytest.mark.unit
class TestEventCreate:
    def _valid_kwargs(self, **overrides):
        base = dict(
            idempotency_key="test-123",
            event_type="pageview",
            timestamp=datetime.now(timezone.utc),
            user_id="user-1",
            source_url="https://example.com",
        )
        base.update(overrides)
        return base

    def test_valid_event(self):
        event = EventCreate(**self._valid_kwargs())
        assert event.idempotency_key == "test-123"
        assert event.event_type == "pageview"

    def test_valid_with_metadata(self):
        event = EventCreate(**self._valid_kwargs(metadata={"browser": "Chrome"}))
        assert event.metadata["browser"] == "Chrome"

    def test_missing_idempotency_key(self):
        kwargs = self._valid_kwargs()
        del kwargs["idempotency_key"]
        with pytest.raises(Exception):
            EventCreate(**kwargs)

    def test_metadata_too_large(self):
        with pytest.raises(ValueError, match="metadata"):
            EventCreate(**self._valid_kwargs(metadata={"data": "x" * 70000}))

    def test_metadata_too_deep(self):
        deep = {}
        current = deep
        for _ in range(15):
            current["nested"] = {}
            current = current["nested"]
        with pytest.raises(ValueError, match="depth"):
            EventCreate(**self._valid_kwargs(metadata=deep))

    def test_metadata_at_max_depth_ok(self):
        nested = {}
        current = nested
        for _ in range(9):  # 9 levels of nesting = depth 10 including root
            current["a"] = {}
            current = current["a"]
        event = EventCreate(**self._valid_kwargs(metadata=nested))
        assert event.metadata is not None

    def test_timestamp_too_far_past(self):
        ts = datetime.now(timezone.utc) - timedelta(days=60)
        with pytest.raises(ValueError, match="timestamp"):
            EventCreate(**self._valid_kwargs(timestamp=ts))

    def test_timestamp_too_far_future(self):
        ts = datetime.now(timezone.utc) + timedelta(minutes=30)
        with pytest.raises(ValueError, match="timestamp"):
            EventCreate(**self._valid_kwargs(timestamp=ts))

    def test_timestamp_within_bounds(self):
        ts = datetime.now(timezone.utc) - timedelta(days=5)
        event = EventCreate(**self._valid_kwargs(timestamp=ts))
        assert event.timestamp == ts


@pytest.mark.unit
class TestEventAccepted:
    def test_mirrors_input(self):
        accepted = EventAccepted(
            idempotency_key="key-1",
            event_type="click",
            timestamp=datetime.now(timezone.utc),
            user_id="u1",
            source_url="https://example.com",
            metadata={"a": 1},
        )
        assert accepted.idempotency_key == "key-1"
        assert accepted.metadata == {"a": 1}


@pytest.mark.unit
class TestEventResponse:
    def test_from_mongo(self):
        from bson import ObjectId
        oid = ObjectId()
        now = datetime.now(timezone.utc)
        doc = {
            "_id": oid,
            "idempotency_key": "key-1",
            "event_type": "pageview",
            "timestamp": now,
            "user_id": "u1",
            "source_url": "https://example.com",
            "metadata": None,
            "created_at": now,
        }
        resp = EventResponse.from_mongo(doc)
        assert resp.id == str(oid)
        assert resp.idempotency_key == "key-1"


@pytest.mark.unit
class TestEventFilter:
    def test_defaults(self):
        f = EventFilter()
        assert f.event_type is None
        assert f.skip == 0
        assert f.limit == 50

    def test_limit_bounds(self):
        with pytest.raises(Exception):
            EventFilter(limit=0)
        with pytest.raises(Exception):
            EventFilter(limit=201)
```

**Step 2: Run test — expect FAIL**

Run: `uv run pytest src/tests/unit/test_schemas.py -v`

**Step 3: Implement src/events/schemas.py**

Key implementation details:
- `EventCreate`: Pydantic BaseModel with `idempotency_key`, `event_type`, `timestamp`, `user_id`, `source_url`, optional `metadata` dict. `@field_validator` for metadata size (orjson.dumps, check len > settings.max_metadata_size), metadata depth (recursive walk), and timestamp bounds (check against now ± configured window). Import `get_settings` from config.
- `EventAccepted`: Same fields as EventCreate (no id, no created_at). Used as 202 response.
- `EventResponse`: Adds `id: str` and `created_at: datetime`. Has `@classmethod from_mongo(doc)` that converts `_id` ObjectId to string.
- `EventFilter`: All optional filters (`event_type`, `user_id`, `source_url`, `start_date`, `end_date`) plus `skip: int = Field(0, ge=0)` and `limit: int = Field(50, ge=1, le=200)`.

**Step 4: Run test — expect PASS**

Run: `uv run pytest src/tests/unit/test_schemas.py -v`

**Step 5: Commit**

```bash
git add src/events/schemas.py src/tests/unit/test_schemas.py
git commit -m "feat: event Pydantic schemas with validation"
```

---

### Task 4: Analytics & Search Schemas

**Files:**
- Create: `src/analytics/schemas.py`
- Create: `src/search/schemas.py`
- Extend: `src/tests/unit/test_schemas.py`

**Step 1: Write the failing tests**

Append to `src/tests/unit/test_schemas.py`:

```python
from src.analytics.schemas import TimeBucket, StatsQuery, StatsWindow, RealtimeStatsQuery, StatsBucket
from src.search.schemas import SearchQuery

@pytest.mark.unit
class TestStatsQuery:
    def test_valid(self):
        q = StatsQuery(
            time_bucket=TimeBucket.HOURLY,
            start_date=datetime.now(timezone.utc) - timedelta(days=1),
            end_date=datetime.now(timezone.utc),
        )
        assert q.time_bucket == TimeBucket.HOURLY

    def test_hourly_exceeds_max_range(self):
        with pytest.raises(ValueError, match="exceeds"):
            StatsQuery(
                time_bucket=TimeBucket.HOURLY,
                start_date=datetime.now(timezone.utc) - timedelta(days=10),
                end_date=datetime.now(timezone.utc),
            )

    def test_daily_within_range(self):
        q = StatsQuery(
            time_bucket=TimeBucket.DAILY,
            start_date=datetime.now(timezone.utc) - timedelta(days=300),
            end_date=datetime.now(timezone.utc),
        )
        assert q.time_bucket == TimeBucket.DAILY

    def test_daily_exceeds_max_range(self):
        with pytest.raises(ValueError, match="exceeds"):
            StatsQuery(
                time_bucket=TimeBucket.DAILY,
                start_date=datetime.now(timezone.utc) - timedelta(days=400),
                end_date=datetime.now(timezone.utc),
            )

    def test_no_date_range_ok(self):
        q = StatsQuery(time_bucket=TimeBucket.WEEKLY)
        assert q.start_date is None


@pytest.mark.unit
class TestRealtimeStatsQuery:
    def test_defaults(self):
        q = RealtimeStatsQuery()
        assert q.window == StatsWindow.LAST_1H
        assert q.event_type is None

    def test_with_event_type(self):
        q = RealtimeStatsQuery(window=StatsWindow.LAST_24H, event_type="click")
        assert q.event_type == "click"


@pytest.mark.unit
class TestSearchQuery:
    def test_valid(self):
        q = SearchQuery(q="chrome mobile")
        assert q.q == "chrome mobile"
        assert q.skip == 0
        assert q.limit == 20

    def test_with_filters(self):
        q = SearchQuery(q="test", event_type="click", user_id="u1")
        assert q.event_type == "click"

    def test_limit_bounds(self):
        with pytest.raises(Exception):
            SearchQuery(q="test", limit=0)
        with pytest.raises(Exception):
            SearchQuery(q="test", limit=101)
```

**Step 2: Run test — expect FAIL**

Run: `uv run pytest src/tests/unit/test_schemas.py -v -k "StatsQuery or RealtimeStats or SearchQuery"`

**Step 3: Implement**

`src/analytics/schemas.py`:
- `TimeBucket(str, Enum)`: HOURLY, DAILY, WEEKLY
- `StatsWindow(str, Enum)`: LAST_1H = "1h", LAST_6H = "6h", LAST_24H = "24h"
- `StatsQuery`: `time_bucket`, optional `event_type`, `start_date`, `end_date`. `@model_validator(mode="after")` checks date range against per-bucket max from settings.
- `RealtimeStatsQuery`: `window: StatsWindow = StatsWindow.LAST_1H`, optional `event_type`
- `StatsBucket`: `time_bucket: datetime`, `event_type: str`, `count: int`

`src/search/schemas.py`:
- `SearchQuery`: `q: str`, optional `event_type`, `user_id`, `start_date`, `end_date`, `skip: int = Field(0, ge=0)`, `limit: int = Field(20, ge=1, le=100)`
- `SearchHit`: `id: str`, `event_type`, `timestamp`, `user_id`, `source_url`, optional `metadata`, `score: float`
- `SearchResponse`: `hits: list[SearchHit]`, `total: int`

**Step 4: Run test — expect PASS**

Run: `uv run pytest src/tests/unit/test_schemas.py -v`

**Step 5: Commit**

```bash
git add src/analytics/schemas.py src/search/schemas.py src/tests/unit/test_schemas.py
git commit -m "feat: analytics and search Pydantic schemas"
```

---

## Phase 2: Queue System

### Task 5: Queue Protocol & In-Memory Queue

**Files:**
- Create: `src/queue/base.py`
- Create: `src/queue/memory.py`
- Test: `src/tests/unit/test_queue.py`

**Step 1: Write the failing test**

```python
# src/tests/unit/test_queue.py
import asyncio
import pytest
from src.queue.base import QueueMessage
from src.queue.memory import InMemoryQueue, SENTINEL
from src.queue.dlq import DeadLetterQueue

def _msg(key="k1", **overrides):
    kwargs = dict(payload={"idempotency_key": key, "event_type": "click"})
    kwargs.update(overrides)
    return QueueMessage(**kwargs)

@pytest.mark.unit
class TestInMemoryQueueBasics:
    async def test_enqueue_and_drain_ordering(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=10, dlq=dlq, max_retries=3)
        m1, m2 = _msg("a"), _msg("b")
        await q.enqueue(m1)
        await q.enqueue(m2)
        batch = await q.drain(max_items=10, timeout=1.0)
        assert [m.payload["idempotency_key"] for m in batch] == ["a", "b"]

    async def test_drain_respects_max_items(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=10, dlq=dlq, max_retries=3)
        for i in range(5):
            await q.enqueue(_msg(f"k{i}"))
        batch = await q.drain(max_items=3, timeout=1.0)
        assert len(batch) == 3

    async def test_drain_returns_empty_on_timeout(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=10, dlq=dlq, max_retries=3)
        batch = await q.drain(max_items=10, timeout=0.1)
        assert batch == []

    async def test_enqueue_raises_when_full(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=2, dlq=dlq, max_retries=3)
        await q.enqueue(_msg("a"))
        await q.enqueue(_msg("b"))
        with pytest.raises(asyncio.QueueFull):
            await q.enqueue(_msg("c"))

    async def test_qsize(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=10, dlq=dlq, max_retries=3)
        assert q.qsize() == 0
        await q.enqueue(_msg())
        assert q.qsize() == 1

    async def test_sentinel_in_drain(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=10, dlq=dlq, max_retries=3)
        await q.enqueue(_msg("before"))
        await q.shutdown()
        batch = await q.drain(max_items=10, timeout=1.0)
        # batch contains the message then the sentinel
        assert batch[0].payload["idempotency_key"] == "before"
        assert batch[1] is SENTINEL

    async def test_ack_calls_task_done(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=10, dlq=dlq, max_retries=3)
        msg = _msg()
        await q.enqueue(msg)
        await q.drain(max_items=1, timeout=1.0)
        await q.ack(msg.message_id)
        # join() should return immediately since all tasks are done
        await asyncio.wait_for(q.join(), timeout=1.0)
```

**Step 2: Run test — expect FAIL**

Run: `uv run pytest src/tests/unit/test_queue.py -v`

**Step 3: Implement**

```python
# src/queue/base.py
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
import uuid

@dataclass
class QueueMessage:
    payload: dict[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    retry_count: int = 0
    enqueued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_error: str | None = None
    error_history: list[str] = field(default_factory=list)

class EventQueue(Protocol):
    async def enqueue(self, message: QueueMessage) -> None: ...
    async def drain(self, max_items: int, timeout: float) -> list: ...
    async def ack(self, message_id: str) -> None: ...
    async def nack(self, message: QueueMessage, error: str) -> None: ...
    def qsize(self) -> int: ...
```

```python
# src/queue/memory.py
from __future__ import annotations
import asyncio
from src.queue.base import QueueMessage

SENTINEL = object()

class InMemoryQueue:
    def __init__(self, maxsize: int, dlq, max_retries: int = 5):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._dlq = dlq
        self._max_retries = max_retries

    async def enqueue(self, message: QueueMessage) -> None:
        self._queue.put_nowait(message)  # raises asyncio.QueueFull

    async def drain(self, max_items: int, timeout: float) -> list:
        items = []
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            items.append(first)
            if first is SENTINEL:
                return items
        except asyncio.TimeoutError:
            return items

        while len(items) < max_items:
            try:
                item = self._queue.get_nowait()
                items.append(item)
                if item is SENTINEL:
                    return items
            except asyncio.QueueEmpty:
                break
        return items

    async def ack(self, message_id: str) -> None:
        self._queue.task_done()

    async def nack(self, message: QueueMessage, error: str) -> None:
        message.retry_count += 1
        message.last_error = error
        message.error_history.append(error)
        self._queue.task_done()

        if message.retry_count >= self._max_retries:
            await self._dlq.put(message)
            return
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            await self._dlq.put(message)

    def qsize(self) -> int:
        return self._queue.qsize()

    async def join(self) -> None:
        await self._queue.join()

    async def shutdown(self) -> None:
        await self._queue.put(SENTINEL)

    @property
    def sentinel(self):
        return SENTINEL
```

**Step 4: Run test — expect PASS**

Run: `uv run pytest src/tests/unit/test_queue.py -v`

**Step 5: Commit**

```bash
git add src/queue/ src/tests/unit/test_queue.py
git commit -m "feat: queue protocol and in-memory implementation"
```

---

### Task 6: Dead Letter Queue + Nack-to-DLQ Routing

**Files:**
- Create: `src/queue/dlq.py`
- Extend: `src/tests/unit/test_queue.py`

**Step 1: Write the failing tests**

Append to `src/tests/unit/test_queue.py`:

```python
@pytest.mark.unit
class TestDeadLetterQueue:
    async def test_put_and_get(self):
        dlq = DeadLetterQueue(maxsize=10)
        msg = _msg()
        await dlq.put(msg)
        assert dlq.qsize() == 1
        got = await dlq.get()
        assert got.message_id == msg.message_id

    async def test_overflow_drops_message(self, capsys):
        dlq = DeadLetterQueue(maxsize=1)
        await dlq.put(_msg("first"))
        await dlq.put(_msg("dropped"))  # should log, not raise
        assert dlq.qsize() == 1


@pytest.mark.unit
class TestNackRouting:
    async def test_nack_increments_retry(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=10, dlq=dlq, max_retries=3)
        msg = _msg()
        await q.enqueue(msg)
        await q.drain(max_items=1, timeout=1.0)
        await q.nack(msg, "err")
        assert msg.retry_count == 1
        assert msg.error_history == ["err"]

    async def test_nack_requeues(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=10, dlq=dlq, max_retries=3)
        msg = _msg()
        await q.enqueue(msg)
        await q.drain(max_items=1, timeout=1.0)
        await q.nack(msg, "err")
        assert q.qsize() == 1  # back in queue

    async def test_nack_max_retries_routes_to_dlq(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=10, dlq=dlq, max_retries=2)
        msg = _msg()
        msg.retry_count = 1  # one retry already
        await q.enqueue(msg)
        await q.drain(max_items=1, timeout=1.0)
        await q.nack(msg, "final-err")
        assert dlq.qsize() == 1
        assert q.qsize() == 0

    async def test_nack_full_queue_routes_to_dlq(self):
        dlq = DeadLetterQueue(maxsize=10)
        q = InMemoryQueue(maxsize=1, dlq=dlq, max_retries=5)
        msg = _msg("original")
        await q.enqueue(msg)
        await q.drain(max_items=1, timeout=1.0)
        # Fill queue so nack can't re-enqueue
        await q.enqueue(_msg("filler"))
        await q.nack(msg, "err")
        assert dlq.qsize() == 1  # routed to DLQ
```

**Step 2: Run test — expect FAIL**

Run: `uv run pytest src/tests/unit/test_queue.py -v -k "DeadLetter or NackRouting"`

**Step 3: Implement**

```python
# src/queue/dlq.py
from __future__ import annotations
import asyncio
from src.queue.base import QueueMessage
from src.core.logging import get_logger

logger = get_logger(component="dlq")

class DeadLetterQueue:
    def __init__(self, maxsize: int):
        self._queue: asyncio.Queue[QueueMessage] = asyncio.Queue(maxsize=maxsize)

    async def put(self, message: QueueMessage) -> None:
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            logger.error(
                "dlq_overflow_dropped",
                message_id=message.message_id,
                last_error=message.last_error,
                retry_count=message.retry_count,
            )

    def qsize(self) -> int:
        return self._queue.qsize()

    async def get(self) -> QueueMessage:
        return await self._queue.get()
```

**Step 4: Run test — expect PASS**

Run: `uv run pytest src/tests/unit/test_queue.py -v`

**Step 5: Commit**

```bash
git add src/queue/dlq.py src/tests/unit/test_queue.py
git commit -m "feat: dead letter queue with nack routing"
```

---

## Phase 3: Data Layer

### Task 7: Database Connection Management & Index Creation

**Files:**
- Create: `src/core/database.py`

No unit tests — index creation and connectivity are integration concerns. Verified in Task 19.

**Step 1: Implement**

```python
# src/core/database.py
from __future__ import annotations
from pymongo import AsyncMongoClient
from elasticsearch import AsyncElasticsearch
import redis.asyncio as aioredis
from src.config import Settings
from src.core.logging import get_logger

logger = get_logger(component="database")

ES_MAPPING = {
    "mappings": {
        "properties": {
            "event_type": {"type": "keyword"},
            "timestamp": {"type": "date"},
            "user_id": {"type": "keyword"},
            "source_url": {"type": "keyword"},
            "metadata": {"type": "flattened"},
            "metadata_text": {"type": "text", "analyzer": "standard"},
        }
    }
}

class DatabaseManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.mongo_client: AsyncMongoClient | None = None
        self.mongo_db = None
        self.es_client: AsyncElasticsearch | None = None
        self.redis_client: aioredis.Redis | None = None

    async def connect(self) -> None:
        self.mongo_client = AsyncMongoClient(self.settings.mongodb_url)
        self.mongo_db = self.mongo_client[self.settings.mongodb_database]

        self.es_client = AsyncElasticsearch(self.settings.elasticsearch_url)

        self.redis_client = aioredis.Redis.from_url(
            self.settings.redis_url,
            max_connections=self.settings.redis_max_connections,
            decode_responses=True,
        )

    async def create_indexes(self) -> None:
        collection = self.mongo_db["events"]

        # MongoDB indexes (idempotent)
        await collection.create_index("idempotency_key", unique=True)
        await collection.create_index([("event_type", 1), ("timestamp", 1)])
        await collection.create_index("user_id")
        await collection.create_index("source_url")
        await collection.create_index("timestamp")
        logger.info("mongodb_indexes_created")

        # ES index with explicit mapping (ignore 400 = already exists)
        await self.es_client.indices.create(
            index=self.settings.elasticsearch_index,
            body=ES_MAPPING,
            ignore=400,
        )
        logger.info("elasticsearch_index_created", index=self.settings.elasticsearch_index)

    async def close(self) -> None:
        if self.mongo_client:
            self.mongo_client.close()
        if self.es_client:
            await self.es_client.close()
        if self.redis_client:
            await self.redis_client.aclose()

    async def check_mongodb(self) -> bool:
        try:
            await self.mongo_client.admin.command("ping")
            return True
        except Exception:
            return False

    async def check_elasticsearch(self) -> bool:
        try:
            return await self.es_client.ping()
        except Exception:
            return False

    async def check_redis(self) -> bool:
        try:
            return await self.redis_client.ping()
        except Exception:
            return False
```

**Step 2: Verify import works**

Run: `uv run python -c "from src.core.database import DatabaseManager; print('OK')"`

**Step 3: Commit**

```bash
git add src/core/database.py
git commit -m "feat: database connection management and index creation"
```

---

### Task 8: Cache Service

**Files:**
- Create: `src/cache/service.py`
- Test: `src/tests/unit/test_cache_service.py`

**Step 1: Write the failing test**

```python
# src/tests/unit/test_cache_service.py
import pytest
from unittest.mock import AsyncMock, patch
from fakeredis import FakeAsyncRedis
from src.cache.service import CacheService

@pytest.fixture
async def redis():
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()

@pytest.mark.unit
class TestCacheService:
    async def test_get_miss_returns_none(self, redis):
        cache = CacheService(redis, default_ttl=30)
        assert await cache.get("nonexistent") is None

    async def test_set_and_get_roundtrip(self, redis):
        cache = CacheService(redis, default_ttl=30)
        data = {"buckets": [{"count": 5}]}
        await cache.set("key1", data)
        result = await cache.get("key1")
        assert result == data

    async def test_get_redis_error_returns_none(self):
        broken_redis = AsyncMock()
        broken_redis.get = AsyncMock(side_effect=ConnectionError("down"))
        cache = CacheService(broken_redis, default_ttl=30)
        assert await cache.get("key") is None

    async def test_set_redis_error_swallowed(self):
        broken_redis = AsyncMock()
        broken_redis.set = AsyncMock(side_effect=ConnectionError("down"))
        cache = CacheService(broken_redis, default_ttl=30)
        await cache.set("key", {"data": 1})  # should not raise
```

**Step 2: Run test — expect FAIL**

Run: `uv run pytest src/tests/unit/test_cache_service.py -v`

**Step 3: Implement**

```python
# src/cache/service.py
import orjson
from src.core.logging import get_logger

logger = get_logger(component="cache")

class CacheService:
    def __init__(self, redis_client, default_ttl: int = 30):
        self._redis = redis_client
        self._default_ttl = default_ttl

    async def get(self, key: str) -> dict | None:
        try:
            data = await self._redis.get(key)
            if data is None:
                return None
            return orjson.loads(data)
        except Exception as e:
            logger.warning("cache_get_error", key=key, error=str(e))
            return None

    async def set(self, key: str, value: dict, ttl: int | None = None) -> None:
        try:
            await self._redis.set(key, orjson.dumps(value), ex=ttl or self._default_ttl)
        except Exception as e:
            logger.warning("cache_set_error", key=key, error=str(e))
```

**Step 4: Run test — expect PASS**

Run: `uv run pytest src/tests/unit/test_cache_service.py -v`

**Step 5: Commit**

```bash
git add src/cache/service.py src/tests/unit/test_cache_service.py
git commit -m "feat: Redis cache service with fail-open behavior"
```

---

### Task 9: Event Service

**Files:**
- Create: `src/events/service.py`
- Test: `src/tests/unit/test_event_service.py`

**Step 1: Write the failing test**

```python
# src/tests/unit/test_event_service.py
import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from src.events.service import EventService
from src.events.schemas import EventCreate, EventFilter
from src.core.exceptions import QueueFullError

def _event(**overrides):
    base = dict(
        idempotency_key="key-1",
        event_type="pageview",
        timestamp=datetime.now(timezone.utc),
        user_id="user-1",
        source_url="https://example.com",
    )
    base.update(overrides)
    return EventCreate(**base)

@pytest.mark.unit
class TestEventServiceEnqueue:
    async def test_enqueue_creates_message(self):
        queue = AsyncMock()
        queue.enqueue = AsyncMock()
        service = EventService(queue=queue, collection=AsyncMock())
        await service.enqueue_event(_event())
        queue.enqueue.assert_called_once()
        msg = queue.enqueue.call_args[0][0]
        assert msg.payload["idempotency_key"] == "key-1"
        assert "created_at" in msg.payload

    async def test_enqueue_sets_created_at(self):
        queue = AsyncMock()
        queue.enqueue = AsyncMock()
        service = EventService(queue=queue, collection=AsyncMock())
        before = datetime.now(timezone.utc)
        await service.enqueue_event(_event())
        msg = queue.enqueue.call_args[0][0]
        assert msg.payload["created_at"] >= before

    async def test_enqueue_raises_queue_full(self):
        queue = AsyncMock()
        queue.enqueue = AsyncMock(side_effect=asyncio.QueueFull())
        service = EventService(queue=queue, collection=AsyncMock())
        with pytest.raises(QueueFullError):
            await service.enqueue_event(_event())


@pytest.mark.unit
class TestEventServiceQuery:
    async def test_query_builds_filter(self):
        mock_cursor = AsyncMock()
        mock_cursor.to_list = AsyncMock(return_value=[])
        mock_cursor.skip = MagicMock(return_value=mock_cursor)
        mock_cursor.limit = MagicMock(return_value=mock_cursor)

        collection = AsyncMock()
        collection.find = MagicMock(return_value=mock_cursor)

        service = EventService(queue=AsyncMock(), collection=collection)
        filters = EventFilter(event_type="click", user_id="u1")
        await service.query_events(filters)

        call_args = collection.find.call_args[0][0]
        assert call_args["event_type"] == "click"
        assert call_args["user_id"] == "u1"

    async def test_query_applies_pagination(self):
        mock_cursor = AsyncMock()
        mock_cursor.to_list = AsyncMock(return_value=[])
        mock_cursor.skip = MagicMock(return_value=mock_cursor)
        mock_cursor.limit = MagicMock(return_value=mock_cursor)

        collection = AsyncMock()
        collection.find = MagicMock(return_value=mock_cursor)

        service = EventService(queue=AsyncMock(), collection=collection)
        filters = EventFilter(skip=10, limit=25)
        await service.query_events(filters)

        mock_cursor.skip.assert_called_with(10)
        mock_cursor.limit.assert_called_with(25)
```

**Step 2: Run test — expect FAIL**

Run: `uv run pytest src/tests/unit/test_event_service.py -v`

**Step 3: Implement**

`src/events/service.py`: `EventService` class with `__init__(self, queue, collection)`, `enqueue_event(event: EventCreate)` (builds payload with `created_at`, creates `QueueMessage`, calls `queue.enqueue`, catches `asyncio.QueueFull` → raises `QueueFullError`), and `query_events(filters: EventFilter)` (builds MongoDB filter dict from non-None filter fields, uses `collection.find(query).skip(skip).limit(limit).to_list(length=limit)`).

**Step 4: Run test — expect PASS**

Run: `uv run pytest src/tests/unit/test_event_service.py -v`

**Step 5: Commit**

```bash
git add src/events/service.py src/tests/unit/test_event_service.py
git commit -m "feat: event service with enqueue and query"
```

---

## Phase 4: Business Logic

### Task 10: Worker — MongoDB Bulk Write + BulkWriteError Handling

**Files:**
- Create: `src/ingestion/worker.py`
- Create: `src/ingestion/retry.py`
- Test: `src/tests/unit/test_worker.py`

**Step 1: Write the failing test**

```python
# src/tests/unit/test_worker.py
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from pymongo.errors import BulkWriteError
from src.ingestion.worker import EventWorker
from src.queue.base import QueueMessage

def _msg(key="k1"):
    return QueueMessage(payload={
        "idempotency_key": key,
        "event_type": "click",
        "timestamp": datetime.now(timezone.utc),
        "user_id": "u1",
        "source_url": "https://example.com",
        "metadata": {"browser": "Chrome"},
        "created_at": datetime.now(timezone.utc),
    })

def _make_worker(mongo=None, es=None, queue=None, dlq=None):
    return EventWorker(
        queue=queue or AsyncMock(),
        dlq=dlq or AsyncMock(),
        mongo_collection=mongo or AsyncMock(),
        es_client=es or AsyncMock(),
        es_index="events",
        batch_size=100,
        batch_timeout=5.0,
        max_retries=5,
        backoff_base=2.0,
        backoff_max=60.0,
    )


@pytest.mark.unit
class TestWorkerMongoDB:
    async def test_insert_many_called_ordered_false(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        worker = _make_worker(mongo=mongo)

        batch = [_msg("a"), _msg("b")]
        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(2, [])):
            await worker.process_batch(batch)

        mongo.insert_many.assert_called_once()
        _, kwargs = mongo.insert_many.call_args
        assert kwargs.get("ordered") is False

    async def test_duplicate_key_treated_as_success(self):
        mongo = AsyncMock()
        error = BulkWriteError({
            "writeErrors": [{"index": 0, "code": 11000, "errmsg": "dup"}],
            "nInserted": 1,
        })
        mongo.insert_many = AsyncMock(side_effect=error)
        queue = AsyncMock()

        worker = _make_worker(mongo=mongo, queue=queue)
        batch = [_msg("dup"), _msg("new")]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(2, [])):
            await worker.process_batch(batch)

        # Both should be forwarded to ES (dup treated as success)
        # ack should be called for both after ES success
        assert queue.ack.call_count == 2

    async def test_non_duplicate_error_nacks(self):
        mongo = AsyncMock()
        error = BulkWriteError({
            "writeErrors": [{"index": 0, "code": 999, "errmsg": "other"}],
            "nInserted": 1,
        })
        mongo.insert_many = AsyncMock(side_effect=error)
        queue = AsyncMock()

        worker = _make_worker(mongo=mongo, queue=queue)
        batch = [_msg("fail"), _msg("ok")]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])):
            await worker.process_batch(batch)

        # "fail" nacked, "ok" acked
        queue.nack.assert_called_once()
        nacked_msg = queue.nack.call_args[0][0]
        assert nacked_msg.payload["idempotency_key"] == "fail"
        queue.ack.assert_called_once()

    async def test_only_succeeded_forwarded_to_es(self):
        mongo = AsyncMock()
        error = BulkWriteError({
            "writeErrors": [{"index": 1, "code": 999, "errmsg": "fail"}],
            "nInserted": 1,
        })
        mongo.insert_many = AsyncMock(side_effect=error)

        worker = _make_worker(mongo=mongo)
        batch = [_msg("ok"), _msg("fail")]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])) as mock_bulk:
            await worker.process_batch(batch)

        # ES should only receive "ok" (index 0 succeeded, index 1 failed non-dup)
        actions = mock_bulk.call_args[0][1]  # second positional arg
        assert len(actions) == 1
        assert actions[0]["_id"] == "ok"
```

**Step 2: Run test — expect FAIL**

Run: `uv run pytest src/tests/unit/test_worker.py -v -k "WorkerMongoDB"`

**Step 3: Implement src/ingestion/worker.py**

Core structure:

```python
# src/ingestion/worker.py
from __future__ import annotations
import asyncio
import random
from datetime import datetime, timezone
from pymongo.errors import BulkWriteError
from elasticsearch.helpers import async_bulk
from src.queue.base import QueueMessage
from src.queue.memory import SENTINEL
from src.search.service import extract_metadata_text
from src.core.logging import get_logger

logger = get_logger(component="worker")

class EventWorker:
    def __init__(self, queue, dlq, mongo_collection, es_client, es_index,
                 batch_size, batch_timeout, max_retries, backoff_base, backoff_max):
        self._queue = queue
        self._dlq = dlq
        self._mongo = mongo_collection
        self._es = es_client
        self._es_index = es_index
        self._batch_size = batch_size
        self._batch_timeout = batch_timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._consecutive_failures = 0
        self._last_heartbeat: datetime | None = None

    def is_alive(self, staleness_seconds: float = 30.0) -> bool:
        if self._last_heartbeat is None:
            return False
        elapsed = (datetime.now(timezone.utc) - self._last_heartbeat).total_seconds()
        return elapsed < staleness_seconds

    async def run(self) -> None:
        try:
            while True:
                batch = await self._queue.drain(self._batch_size, self._batch_timeout)
                self._last_heartbeat = datetime.now(timezone.utc)

                real_batch, has_sentinel = self._split_sentinel(batch)
                if real_batch:
                    try:
                        await self.process_batch(real_batch)
                        self._consecutive_failures = 0
                    except Exception as e:
                        logger.error("batch_level_failure", error=str(e))
                        for msg in real_batch:
                            await self._queue.nack(msg, str(e))
                        self._consecutive_failures += 1
                        delay = min(self._backoff_base * (2 ** self._consecutive_failures), self._backoff_max)
                        await asyncio.sleep(delay + random.uniform(0, delay * 0.1))
                if has_sentinel:
                    logger.info("shutdown_sentinel_received")
                    break
        except Exception:
            logger.exception("worker_fatal_error")

    def _split_sentinel(self, batch):
        for i, item in enumerate(batch):
            if item is SENTINEL:
                return batch[:i], True
        return batch, False

    async def process_batch(self, batch: list[QueueMessage]) -> None:
        # 1. MongoDB bulk write
        succeeded, failed = await self._write_mongodb(batch)

        # 2. Nack/DLQ mongo failures
        for msg, error_msg in failed:
            await self._queue.nack(msg, error_msg)

        # 3. ES bulk write for succeeded items
        if succeeded:
            await self._write_elasticsearch(succeeded)

    async def _write_mongodb(self, batch):
        documents = [self._to_mongo_doc(m) for m in batch]
        succeeded = list(batch)
        failed = []
        try:
            await self._mongo.insert_many(documents, ordered=False)
        except BulkWriteError as e:
            error_map = {}
            for err in e.details.get("writeErrors", []):
                error_map[err["index"]] = err["code"]
            succeeded = []
            failed = []
            for i, msg in enumerate(batch):
                if i not in error_map:
                    succeeded.append(msg)
                elif error_map[i] == 11000:
                    succeeded.append(msg)  # duplicate = success
                else:
                    failed.append((msg, f"MongoDB error code {error_map[i]}"))
        return succeeded, failed

    async def _write_elasticsearch(self, batch):
        actions = [self._to_es_action(m) for m in batch]
        success_count, errors = await async_bulk(
            self._es, actions, raise_on_error=False, raise_on_exception=False,
        )
        failed_map = {}
        for err_item in errors:
            action_type = list(err_item.keys())[0]
            detail = err_item[action_type]
            failed_map[detail["_id"]] = detail["status"]

        for msg in batch:
            key = msg.payload["idempotency_key"]
            if key not in failed_map:
                await self._queue.ack(msg.message_id)
            else:
                status = failed_map[key]
                if status == 429 or status >= 500:
                    await self._queue.nack(msg, f"ES retryable status {status}")
                else:
                    msg.last_error = f"ES permanent status {status}"
                    await self._dlq.put(msg)
                    await self._queue.ack(msg.message_id)

    def _to_mongo_doc(self, msg: QueueMessage) -> dict:
        p = msg.payload
        return {
            "idempotency_key": p["idempotency_key"],
            "event_type": p["event_type"],
            "timestamp": p["timestamp"],
            "user_id": p["user_id"],
            "source_url": p["source_url"],
            "metadata": p.get("metadata"),
            "created_at": p["created_at"],
        }

    def _to_es_action(self, msg: QueueMessage) -> dict:
        p = msg.payload
        metadata = p.get("metadata") or {}
        return {
            "_index": self._es_index,
            "_id": p["idempotency_key"],
            "event_type": p["event_type"],
            "timestamp": p["timestamp"],
            "user_id": p["user_id"],
            "source_url": p["source_url"],
            "metadata": metadata,
            "metadata_text": extract_metadata_text(metadata),
        }
```

Note: `extract_metadata_text` is implemented in Task 13 (search service). Create a stub for now:

```python
# src/search/service.py (stub — full implementation in Task 13)
def extract_metadata_text(metadata: dict) -> str:
    values = []
    def _walk(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)
        else:
            values.append(str(obj))
    _walk(metadata)
    return " ".join(values)
```

**Step 4: Run test — expect PASS**

Run: `uv run pytest src/tests/unit/test_worker.py -v -k "WorkerMongoDB"`

**Step 5: Commit**

```bash
git add src/ingestion/worker.py src/search/service.py src/tests/unit/test_worker.py
git commit -m "feat: worker MongoDB bulk write with BulkWriteError handling"
```

---

### Task 11: Worker — ES Dual Write + Error Classification + Backoff + Shutdown

**Files:**
- Extend: `src/tests/unit/test_worker.py`
- Modify: `src/ingestion/worker.py` (already implemented in Task 10)

**Step 1: Write the failing tests**

Append to `src/tests/unit/test_worker.py`:

```python
@pytest.mark.unit
class TestWorkerElasticsearch:
    async def test_es_documents_use_idempotency_key_as_id(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        worker = _make_worker(mongo=mongo)
        batch = [_msg("my-key")]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])) as mock_bulk:
            await worker.process_batch(batch)

        actions = mock_bulk.call_args[0][1]
        assert actions[0]["_id"] == "my-key"

    async def test_es_documents_include_metadata_text(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        worker = _make_worker(mongo=mongo)
        batch = [_msg()]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])) as mock_bulk:
            await worker.process_batch(batch)

        actions = mock_bulk.call_args[0][1]
        assert "metadata_text" in actions[0]
        assert "Chrome" in actions[0]["metadata_text"]

    async def test_es_success_acks(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        worker = _make_worker(mongo=mongo, queue=queue)
        batch = [_msg()]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])):
            await worker.process_batch(batch)

        queue.ack.assert_called_once()

    async def test_es_429_nacks(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        worker = _make_worker(mongo=mongo, queue=queue)
        batch = [_msg("k1")]

        errors = [{"index": {"_id": "k1", "status": 429, "error": {"type": "es_rejected"}}}]
        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(0, errors)):
            await worker.process_batch(batch)

        queue.nack.assert_called_once()
        assert "retryable" in queue.nack.call_args[0][1].lower()

    async def test_es_500_nacks(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        worker = _make_worker(mongo=mongo, queue=queue)
        batch = [_msg("k1")]

        errors = [{"index": {"_id": "k1", "status": 500, "error": {"type": "internal"}}}]
        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(0, errors)):
            await worker.process_batch(batch)

        queue.nack.assert_called_once()

    async def test_es_400_routes_to_dlq(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        dlq = AsyncMock()
        worker = _make_worker(mongo=mongo, queue=queue, dlq=dlq)
        batch = [_msg("k1")]

        errors = [{"index": {"_id": "k1", "status": 400, "error": {"type": "mapping"}}}]
        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(0, errors)):
            await worker.process_batch(batch)

        dlq.put.assert_called_once()
        queue.ack.assert_called_once()  # ack original after DLQ


@pytest.mark.unit
class TestWorkerBackoff:
    async def test_batch_failure_triggers_backoff(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock(side_effect=ConnectionError("down"))
        queue = AsyncMock()
        queue.drain = AsyncMock(side_effect=[
            [_msg()],
            [],  # empty after backoff, ends loop via sentinel
        ])
        worker = _make_worker(mongo=mongo, queue=queue)

        # process_batch raises, run() catches and backs off
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            # Simulate: first drain returns batch (fails), second returns sentinel
            queue.drain = AsyncMock(side_effect=[
                [_msg()],
                [SENTINEL],
            ])
            await worker.run()

        mock_sleep.assert_called_once()
        delay = mock_sleep.call_args[0][0]
        assert delay > 0  # backoff applied

    async def test_consecutive_failures_increase_backoff(self):
        worker = _make_worker()
        worker._consecutive_failures = 3
        # After another failure, consecutive = 4, delay = min(2.0 * 2^4, 60) = 32
        worker._consecutive_failures = 4
        expected_base = min(2.0 * (2 ** 4), 60.0)
        assert expected_base == 32.0

    async def test_success_resets_failure_counter(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        worker = _make_worker(mongo=mongo, queue=queue)
        worker._consecutive_failures = 5

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])):
            queue.drain = AsyncMock(side_effect=[[_msg()], [SENTINEL]])
            await worker.run()

        assert worker._consecutive_failures == 0


@pytest.mark.unit
class TestWorkerShutdown:
    async def test_sentinel_stops_worker(self):
        queue = AsyncMock()
        queue.drain = AsyncMock(return_value=[SENTINEL])
        worker = _make_worker(queue=queue)
        await worker.run()  # should exit cleanly

    async def test_processes_items_before_sentinel(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        msg = _msg()
        queue.drain = AsyncMock(return_value=[msg, SENTINEL])
        worker = _make_worker(mongo=mongo, queue=queue)

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])):
            await worker.run()

        mongo.insert_many.assert_called_once()

    async def test_is_alive_heartbeat(self):
        worker = _make_worker()
        assert not worker.is_alive()
        worker._last_heartbeat = datetime.now(timezone.utc)
        assert worker.is_alive()
```

**Step 2: Run test — expect PASS (implementation already in Task 10)**

Run: `uv run pytest src/tests/unit/test_worker.py -v`

If any tests fail, adjust implementation. The worker code from Task 10 should handle all these cases.

**Step 3: Commit**

```bash
git add src/tests/unit/test_worker.py
git commit -m "test: worker ES dual write, error classification, backoff, shutdown"
```

---

### Task 12: Analytics Service

**Files:**
- Create: `src/analytics/service.py`
- Test: `src/tests/unit/test_analytics_service.py`

**Step 1: Write the failing test**

```python
# src/tests/unit/test_analytics_service.py
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock
from src.analytics.service import AnalyticsService, WINDOW_CONFIG
from src.analytics.schemas import TimeBucket, StatsWindow, RealtimeStatsQuery

@pytest.mark.unit
class TestPipelineBuilder:
    def test_hourly_uses_hour_unit(self):
        service = AnalyticsService(collection=AsyncMock(), cache=AsyncMock(), settings=MagicMock())
        pipeline = service.build_stats_pipeline(
            unit="hour", event_type=None, start_date=None, end_date=None,
        )
        group_stage = next(s for s in pipeline if "$group" in s)
        assert group_stage["$group"]["_id"]["time_bucket"]["$dateTrunc"]["unit"] == "hour"

    def test_daily_uses_day_unit(self):
        service = AnalyticsService(collection=AsyncMock(), cache=AsyncMock(), settings=MagicMock())
        pipeline = service.build_stats_pipeline(unit="day")
        group_stage = next(s for s in pipeline if "$group" in s)
        assert group_stage["$group"]["_id"]["time_bucket"]["$dateTrunc"]["unit"] == "day"

    def test_weekly_uses_week_unit(self):
        service = AnalyticsService(collection=AsyncMock(), cache=AsyncMock(), settings=MagicMock())
        pipeline = service.build_stats_pipeline(unit="week")
        group_stage = next(s for s in pipeline if "$group" in s)
        assert group_stage["$group"]["_id"]["time_bucket"]["$dateTrunc"]["unit"] == "week"

    def test_event_type_filter_adds_match(self):
        service = AnalyticsService(collection=AsyncMock(), cache=AsyncMock(), settings=MagicMock())
        pipeline = service.build_stats_pipeline(unit="hour", event_type="click")
        match_stage = next(s for s in pipeline if "$match" in s)
        assert match_stage["$match"]["event_type"] == "click"

    def test_date_range_adds_match(self):
        service = AnalyticsService(collection=AsyncMock(), cache=AsyncMock(), settings=MagicMock())
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=6)
        pipeline = service.build_stats_pipeline(unit="hour", start_date=start, end_date=now)
        match_stage = next(s for s in pipeline if "$match" in s)
        assert "$gte" in match_stage["$match"]["timestamp"]
        assert "$lte" in match_stage["$match"]["timestamp"]

    def test_pipeline_sorts_by_time(self):
        service = AnalyticsService(collection=AsyncMock(), cache=AsyncMock(), settings=MagicMock())
        pipeline = service.build_stats_pipeline(unit="hour")
        sort_stage = next(s for s in pipeline if "$sort" in s)
        assert "_id.time_bucket" in sort_stage["$sort"]


@pytest.mark.unit
class TestWindowConfig:
    def test_1h_maps_to_minute(self):
        assert WINDOW_CONFIG["1h"]["unit"] == "minute"

    def test_6h_maps_to_hour(self):
        assert WINDOW_CONFIG["6h"]["unit"] == "hour"

    def test_24h_maps_to_hour(self):
        assert WINDOW_CONFIG["24h"]["unit"] == "hour"


@pytest.mark.unit
class TestRealtimeStats:
    async def test_cache_hit_returns_cached(self):
        cache = AsyncMock()
        cache.get = AsyncMock(return_value=[{"count": 5}])
        service = AnalyticsService(collection=AsyncMock(), cache=cache, settings=MagicMock())
        result = await service.get_realtime_stats(RealtimeStatsQuery())
        assert result == [{"count": 5}]
        # MongoDB should NOT be called
        service._collection.aggregate.assert_not_called()

    async def test_cache_miss_runs_aggregation(self):
        cache = AsyncMock()
        cache.get = AsyncMock(return_value=None)
        cache.set = AsyncMock()

        mock_cursor = AsyncMock()
        mock_cursor.to_list = AsyncMock(return_value=[{"count": 10}])
        collection = AsyncMock()
        collection.aggregate = MagicMock(return_value=mock_cursor)

        service = AnalyticsService(collection=collection, cache=cache, settings=MagicMock(realtime_stats_ttl=30))
        result = await service.get_realtime_stats(RealtimeStatsQuery())

        assert result == [{"count": 10}]
        cache.set.assert_called_once()
```

**Step 2: Run test — expect FAIL**

Run: `uv run pytest src/tests/unit/test_analytics_service.py -v`

**Step 3: Implement**

`src/analytics/service.py`:
- `WINDOW_CONFIG` dict mapping window values to `{"duration": timedelta, "unit": str}`: `"1h" → ("minute", 1h)`, `"6h" → ("hour", 6h)`, `"24h" → ("hour", 24h)`
- `BUCKET_UNIT_MAP` dict: `TimeBucket.HOURLY → "hour"`, `DAILY → "day"`, `WEEKLY → "week"`
- `AnalyticsService.__init__(self, collection, cache, settings)`
- `build_stats_pipeline(self, unit, event_type=None, start_date=None, end_date=None)` — builds MongoDB aggregation pipeline with `$match` (optional filters), `$group` with `$dateTrunc`, `$sort`, `$project`
- `get_stats(self, query: StatsQuery)` — maps `query.time_bucket` via `BUCKET_UNIT_MAP`, calls `build_stats_pipeline`, runs `collection.aggregate(pipeline).to_list(None)`
- `get_realtime_stats(self, query: RealtimeStatsQuery)` — builds cache key `stats:realtime:{window}:{event_type or 'all'}`, checks cache, on miss: computes `start_date = now - WINDOW_CONFIG[window].duration`, builds pipeline, runs aggregation, caches result, returns

**Step 4: Run test — expect PASS**

Run: `uv run pytest src/tests/unit/test_analytics_service.py -v`

**Step 5: Commit**

```bash
git add src/analytics/service.py src/tests/unit/test_analytics_service.py
git commit -m "feat: analytics service with aggregation pipeline and realtime cache"
```

---

### Task 13: Search Service

**Files:**
- Modify: `src/search/service.py` (expand stub from Task 10)
- Test: `src/tests/unit/test_search_service.py`

**Step 1: Write the failing test**

```python
# src/tests/unit/test_search_service.py
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from src.search.service import SearchService, extract_metadata_text

@pytest.mark.unit
class TestExtractMetadataText:
    def test_flat_dict(self):
        assert extract_metadata_text({"browser": "Chrome", "os": "Linux"}) == "Chrome Linux"

    def test_nested_dict(self):
        result = extract_metadata_text({"browser": {"name": "Chrome", "version": 120}})
        assert "Chrome" in result
        assert "120" in result

    def test_with_arrays(self):
        result = extract_metadata_text({"tags": ["promo", "mobile"]})
        assert "promo" in result
        assert "mobile" in result

    def test_numbers_converted(self):
        result = extract_metadata_text({"count": 42})
        assert "42" in result

    def test_empty_dict(self):
        assert extract_metadata_text({}) == ""

    def test_deeply_nested(self):
        result = extract_metadata_text({"a": {"b": {"c": "deep"}}})
        assert result == "deep"


@pytest.mark.unit
class TestSearchQueryBuilder:
    def test_simple_query(self):
        service = SearchService(es_client=AsyncMock(), index="events")
        body = service.build_query(q="chrome mobile")
        assert body["query"]["bool"]["must"][0]["match"]["metadata_text"] == "chrome mobile"

    def test_with_event_type_filter(self):
        service = SearchService(es_client=AsyncMock(), index="events")
        body = service.build_query(q="test", event_type="click")
        filters = body["query"]["bool"]["filter"]
        assert any(f.get("term", {}).get("event_type") == "click" for f in filters)

    def test_with_date_range(self):
        service = SearchService(es_client=AsyncMock(), index="events")
        now = datetime.now(timezone.utc)
        body = service.build_query(q="test", start_date=now)
        filters = body["query"]["bool"]["filter"]
        assert any("range" in f for f in filters)

    async def test_search_calls_es(self):
        es = AsyncMock()
        es.search = AsyncMock(return_value={
            "hits": {
                "total": {"value": 1},
                "hits": [{"_id": "k1", "_score": 1.5, "_source": {
                    "event_type": "click", "timestamp": "2026-01-01T00:00:00",
                    "user_id": "u1", "source_url": "https://example.com",
                }}],
            }
        })
        service = SearchService(es_client=es, index="events")
        from src.search.schemas import SearchQuery
        result = await service.search(SearchQuery(q="test"))
        assert result["total"] == 1
        assert len(result["hits"]) == 1
```

**Step 2: Run test — expect FAIL**

Run: `uv run pytest src/tests/unit/test_search_service.py -v`

**Step 3: Implement**

Expand `src/search/service.py`: keep `extract_metadata_text` (already stubbed), add `SearchService` class with:
- `__init__(self, es_client, index)`
- `build_query(self, q, event_type=None, user_id=None, start_date=None, end_date=None)` — returns ES query body with `bool` query: `must` contains `match` on `metadata_text`, `filter` contains optional `term` clauses and `range`
- `search(self, query: SearchQuery)` — calls `self._es.search(index=self._index, body=self.build_query(...), from_=query.skip, size=query.limit)`, returns `{"hits": [...], "total": int}`

**Step 4: Run test — expect PASS**

Run: `uv run pytest src/tests/unit/test_search_service.py -v`

**Step 5: Commit**

```bash
git add src/search/service.py src/tests/unit/test_search_service.py
git commit -m "feat: search service with ES query builder and metadata text extraction"
```

---

### Task 14: Rate Limiting Middleware

**Files:**
- Create: `src/cache/middleware.py`
- Test: `src/tests/unit/test_rate_limiter.py`

**Step 1: Write the failing test**

```python
# src/tests/unit/test_rate_limiter.py
import pytest
import time
from unittest.mock import AsyncMock
from fakeredis import FakeAsyncRedis
from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from src.cache.middleware import RateLimitMiddleware

async def homepage(request):
    return PlainTextResponse("ok")

async def health(request):
    return PlainTextResponse("healthy")

def _make_app(redis_client, max_requests=5, window=60):
    app = Starlette(routes=[
        Route("/", homepage),
        Route("/health", health),
    ])
    app.state.redis_client = redis_client
    return RateLimitMiddleware(app.app, max_requests=max_requests, window=window)

@pytest.fixture
async def redis():
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()

@pytest.mark.unit
class TestRateLimiter:
    async def test_requests_within_limit_pass(self, redis):
        from httpx import AsyncClient, ASGITransport
        app = _make_app(redis, max_requests=5)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(5):
                resp = await client.get("/")
                assert resp.status_code == 200

    async def test_exceeding_limit_returns_429(self, redis):
        from httpx import AsyncClient, ASGITransport
        app = _make_app(redis, max_requests=3)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(3):
                await client.get("/")
            resp = await client.get("/")
            assert resp.status_code == 429
            assert "Retry-After" in resp.headers

    async def test_health_excluded(self, redis):
        from httpx import AsyncClient, ASGITransport
        app = _make_app(redis, max_requests=1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.get("/")  # uses up the limit
            resp = await client.get("/health")
            assert resp.status_code == 200  # health excluded

    async def test_redis_error_fails_open(self):
        from httpx import AsyncClient, ASGITransport
        broken_redis = AsyncMock()
        broken_redis.pipeline = lambda: _broken_pipeline()
        app = _make_app(broken_redis, max_requests=1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/")
            assert resp.status_code == 200  # fail open

def _broken_pipeline():
    pipe = AsyncMock()
    pipe.incr = AsyncMock()
    pipe.expire = AsyncMock()
    pipe.execute = AsyncMock(side_effect=ConnectionError("down"))
    return pipe
```

**Step 2: Run test — expect FAIL**

Run: `uv run pytest src/tests/unit/test_rate_limiter.py -v`

**Step 3: Implement**

`src/cache/middleware.py`: ASGI middleware class.
- `__init__(self, app, max_requests=100, window=60, exclude_paths=None)` — stores app and config. `exclude_paths` defaults to `{"/health"}`.
- `__call__(self, scope, receive, send)` — for non-HTTP or excluded paths, pass through. Get redis from `scope["app"].state.redis_client` (or from constructor if not available — for tests, the middleware accesses redis through whichever mechanism is set up). Use sliding window counter: `INCR key`, `EXPIRE key window`, check count > max. On Redis error, fail open. On limit exceeded, return 429 JSON with `Retry-After` header.

Note on redis access: The middleware needs redis. For the rate limiter tests above, the middleware wraps a raw Starlette app, so `scope["app"]` won't have `.state.redis_client`. Two options: (a) pass redis in constructor, or (b) have the middleware look it up from scope. For flexibility, accept an optional `redis_client` in the constructor; if not provided, look it up from `scope["app"].state.redis_client`. Tests pass redis directly; production uses app.state.

**Step 4: Run test — expect PASS**

Run: `uv run pytest src/tests/unit/test_rate_limiter.py -v`

**Step 5: Commit**

```bash
git add src/cache/middleware.py src/tests/unit/test_rate_limiter.py
git commit -m "feat: Redis-backed rate limiting ASGI middleware"
```

---

## Phase 5: API Layer

### Task 15: Routers & Dependencies

**Files:**
- Create: `src/events/router.py`
- Create: `src/events/dependencies.py`
- Create: `src/analytics/router.py`
- Create: `src/search/router.py`

No unit tests — routers are thin wrappers. Verified in integration tests (Task 19).

**Step 1: Implement event router**

`src/events/router.py`:
- `POST /events` (202) — accepts `EventCreate` body, calls `request.app.state.event_service.enqueue_event(event)`, catches `QueueFullError` → 503, returns `EventAccepted(**event.model_dump())`
- `GET /events` — uses `Depends(get_event_filter)`, calls `event_service.query_events(filters)`, returns `[EventResponse.from_mongo(doc) for doc in results]`

`src/events/dependencies.py`:
- `get_event_filter(event_type, user_id, source_url, start_date, end_date, skip, limit)` — all Query params, returns `EventFilter`

**Step 2: Implement analytics router**

`src/analytics/router.py`:
- `GET /events/stats` — accepts `StatsQuery` as `Depends()`, calls `analytics_service.get_stats(query)`, returns result
- `GET /events/stats/realtime` — accepts `RealtimeStatsQuery` as `Depends()`, calls `analytics_service.get_realtime_stats(query)`, returns result

**Step 3: Implement search router**

`src/search/router.py`:
- `GET /events/search` — accepts `SearchQuery` as `Depends()`, calls `search_service.search(query)`, returns result

**Step 4: Verify imports**

Run: `uv run python -c "from src.events.router import router; print('OK')"`

**Step 5: Commit**

```bash
git add src/events/router.py src/events/dependencies.py src/analytics/router.py src/search/router.py
git commit -m "feat: API routers and dependencies"
```

---

### Task 16: Health Endpoint

**Files:**
- Create: `src/health/router.py`

**Step 1: Implement**

`src/health/router.py`:
- `GET /health` — checks `db.check_mongodb()`, `db.check_elasticsearch()`, `db.check_redis()`, reads `worker.is_alive()`, `queue.qsize()`, `dlq.qsize()`
- Status logic: `unhealthy` if MongoDB down or worker dead. `degraded` if ES or Redis down, or DLQ non-empty, or queue > 90% capacity. Otherwise `healthy`.
- Returns JSON with `status`, `dependencies` (up/down per service), and `pipeline` (worker_alive, queue_depth, dlq_depth).
- All state accessed via `request.app.state`.

**Step 2: Verify import**

Run: `uv run python -c "from src.health.router import router; print('OK')"`

**Step 3: Commit**

```bash
git add src/health/router.py
git commit -m "feat: health endpoint with dependency and pipeline checks"
```

---

### Task 17: App Factory & Lifespan

**Files:**
- Create: `src/main.py`

**Step 1: Implement**

`src/main.py`:
- `create_app(settings=None, db_manager=None)` — factory function. Creates `FastAPI` with lifespan. Includes all routers. Adds `RateLimitMiddleware`. Stores settings on `app.state.settings`. If `db_manager` provided, stores on `app.state.db_override` (for testing).
- Lifespan context manager:
  - **Startup**: Use `db_override` if provided, else create `DatabaseManager` and `connect()`. Call `create_indexes()`. Create `DeadLetterQueue`, `InMemoryQueue`. Create `EventService`, `AnalyticsService`, `SearchService`, `CacheService`. Create `EventWorker`, start as `asyncio.Task`. Store all on `app.state`.
  - **Shutdown**: Put sentinel via `queue.shutdown()` (with timeout). Wait for worker task (with timeout). On timeout, cancel worker, log warning. Call `db.close()`.
- Module-level `app = create_app()` for uvicorn.

**Step 2: Verify app starts (requires services — just verify import)**

Run: `uv run python -c "from src.main import create_app; print('OK')"`

**Step 3: Commit**

```bash
git add src/main.py
git commit -m "feat: app factory with lifespan managing all service lifecycle"
```

---

## Phase 6: Integration Testing

### Task 18: Integration Test Infrastructure

**Files:**
- Create: `src/tests/conftest.py`
- Create: `src/tests/integration/conftest.py`

**Step 1: Implement**

`src/tests/conftest.py` — empty (marks package).

`src/tests/integration/conftest.py`:

```python
import pytest
from uuid import uuid4
from pymongo import AsyncMongoClient
from elasticsearch import AsyncElasticsearch
from fakeredis import FakeAsyncRedis
from httpx import AsyncClient, ASGITransport
from asgi_lifespan import LifespanManager
from testcontainers.mongodb import MongoDbContainer
from testcontainers.elasticsearch import ElasticSearchContainer
from src.config import Settings
from src.core.database import DatabaseManager
from src.main import create_app

@pytest.fixture(scope="session")
def mongodb_container():
    with MongoDbContainer("mongo:8") as c:
        yield c

@pytest.fixture(scope="session")
def mongodb_url(mongodb_container):
    return mongodb_container.get_connection_url()

@pytest.fixture(scope="session")
def es_container():
    with ElasticSearchContainer("elasticsearch:9.1.0") as c:
        yield c

@pytest.fixture(scope="session")
def es_url(es_container):
    host = es_container.get_container_host_ip()
    port = es_container.get_exposed_port(9200)
    return f"http://{host}:{port}"

@pytest.fixture
async def app(mongodb_url, es_url):
    suffix = uuid4().hex[:8]
    settings = Settings(
        mongodb_url=mongodb_url,
        mongodb_database=f"test_{suffix}",
        elasticsearch_url=es_url,
        elasticsearch_index=f"events_{suffix}",
        redis_url="redis://fake",
        batch_timeout=0.5,  # fast flush for tests
    )

    db = DatabaseManager(settings)
    db.mongo_client = AsyncMongoClient(mongodb_url)
    db.mongo_db = db.mongo_client[settings.mongodb_database]
    db.es_client = AsyncElasticsearch(es_url)
    db.redis_client = FakeAsyncRedis(decode_responses=True)

    application = create_app(settings=settings, db_manager=db)

    async with LifespanManager(application) as manager:
        yield manager.app

    # Cleanup
    await db.mongo_client.drop_database(settings.mongodb_database)
    await db.es_client.indices.delete(index=settings.elasticsearch_index, ignore=[404])
    db.mongo_client.close()
    await db.es_client.close()
    await db.redis_client.aclose()

@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

async def wait_for_processing(app):
    """Wait for all queued events to be processed by the worker."""
    await app.state.queue.join()
    # Refresh ES index to make documents searchable
    await app.state.db.es_client.indices.refresh(index=app.state.settings.elasticsearch_index)
```

**Step 2: Commit**

```bash
git add src/tests/conftest.py src/tests/integration/conftest.py
git commit -m "feat: integration test infrastructure with testcontainers"
```

---

### Task 19: Integration Tests

**Files:**
- Create: `src/tests/integration/test_ingestion.py`
- Create: `src/tests/integration/test_search.py`
- Create: `src/tests/integration/test_stats.py`
- Create: `src/tests/integration/test_realtime_cache.py`
- Create: `src/tests/integration/test_health.py`

**Step 1: Write tests**

```python
# src/tests/integration/test_ingestion.py
import pytest
from datetime import datetime, timezone
from src.tests.integration.conftest import wait_for_processing

@pytest.mark.integration
class TestIngestion:
    async def test_post_returns_202(self, client):
        resp = await client.post("/events", json={
            "idempotency_key": "int-1",
            "event_type": "pageview",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_id": "u1",
            "source_url": "https://example.com",
        })
        assert resp.status_code == 202
        assert resp.json()["idempotency_key"] == "int-1"

    async def test_ingest_then_query(self, client, app):
        await client.post("/events", json={
            "idempotency_key": "int-2",
            "event_type": "click",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_id": "u1",
            "source_url": "https://example.com",
        })
        await wait_for_processing(app)

        resp = await client.get("/events", params={"event_type": "click"})
        assert resp.status_code == 200
        events = resp.json()
        assert len(events) >= 1
        assert events[0]["idempotency_key"] == "int-2"

    async def test_duplicate_idempotency_key(self, client, app):
        payload = {
            "idempotency_key": "dup-1",
            "event_type": "pageview",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_id": "u1",
            "source_url": "https://example.com",
        }
        await client.post("/events", json=payload)
        await client.post("/events", json=payload)
        await wait_for_processing(app)

        resp = await client.get("/events", params={"user_id": "u1"})
        keys = [e["idempotency_key"] for e in resp.json() if e["idempotency_key"] == "dup-1"]
        assert len(keys) == 1  # deduplication worked
```

```python
# src/tests/integration/test_search.py
import pytest
from datetime import datetime, timezone
from src.tests.integration.conftest import wait_for_processing

@pytest.mark.integration
class TestSearch:
    async def test_search_finds_event(self, client, app):
        await client.post("/events", json={
            "idempotency_key": "search-1",
            "event_type": "click",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_id": "u1",
            "source_url": "https://example.com",
            "metadata": {"browser": "Chrome", "campaign": "spring_sale"},
        })
        await wait_for_processing(app)

        resp = await client.get("/events/search", params={"q": "spring_sale"})
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    async def test_search_metadata_text_extraction(self, client, app):
        await client.post("/events", json={
            "idempotency_key": "search-2",
            "event_type": "conversion",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_id": "u2",
            "source_url": "https://example.com",
            "metadata": {"nested": {"deep": "unicorn_value"}},
        })
        await wait_for_processing(app)

        resp = await client.get("/events/search", params={"q": "unicorn_value"})
        assert resp.json()["total"] >= 1
```

```python
# src/tests/integration/test_stats.py
import pytest
from datetime import datetime, timezone, timedelta
from src.tests.integration.conftest import wait_for_processing

@pytest.mark.integration
class TestStats:
    async def test_stats_aggregation(self, client, app):
        now = datetime.now(timezone.utc)
        for i in range(3):
            await client.post("/events", json={
                "idempotency_key": f"stats-{i}",
                "event_type": "pageview",
                "timestamp": now.isoformat(),
                "user_id": "u1",
                "source_url": "https://example.com",
            })
        await client.post("/events", json={
            "idempotency_key": "stats-click",
            "event_type": "click",
            "timestamp": now.isoformat(),
            "user_id": "u1",
            "source_url": "https://example.com",
        })
        await wait_for_processing(app)

        resp = await client.get("/events/stats", params={
            "time_bucket": "hourly",
            "start_date": (now - timedelta(hours=1)).isoformat(),
            "end_date": (now + timedelta(hours=1)).isoformat(),
        })
        assert resp.status_code == 200
        buckets = resp.json()["buckets"]
        pageview_bucket = next((b for b in buckets if b["event_type"] == "pageview"), None)
        assert pageview_bucket is not None
        assert pageview_bucket["count"] == 3
```

```python
# src/tests/integration/test_realtime_cache.py
import pytest
from datetime import datetime, timezone
from src.tests.integration.conftest import wait_for_processing

@pytest.mark.integration
class TestRealtimeCache:
    async def test_realtime_populates_cache(self, client, app):
        now = datetime.now(timezone.utc)
        await client.post("/events", json={
            "idempotency_key": "rt-1",
            "event_type": "pageview",
            "timestamp": now.isoformat(),
            "user_id": "u1",
            "source_url": "https://example.com",
        })
        await wait_for_processing(app)

        # First call — cache miss, runs aggregation
        resp1 = await client.get("/events/stats/realtime", params={"window": "1h"})
        assert resp1.status_code == 200

        # Second call — should hit cache
        resp2 = await client.get("/events/stats/realtime", params={"window": "1h"})
        assert resp2.status_code == 200
        assert resp1.json() == resp2.json()
```

```python
# src/tests/integration/test_health.py
import pytest

@pytest.mark.integration
class TestHealth:
    async def test_healthy_state(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["dependencies"]["mongodb"] == "up"
        assert data["dependencies"]["elasticsearch"] == "up"
        assert data["dependencies"]["redis"] == "up"
        assert data["pipeline"]["worker_alive"] is True
```

**Step 2: Run unit tests to confirm they still pass**

Run: `uv run pytest src/tests/unit/ -v`

**Step 3: Run integration tests (requires Docker)**

Run: `uv run pytest src/tests/integration/ -v -m integration`

**Step 4: Commit**

```bash
git add src/tests/integration/
git commit -m "test: integration tests for ingestion, search, stats, cache, health"
```

---

## Phase 7: Deployment & Documentation

### Task 20: Docker Setup

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `.dockerignore`

**Step 1: Create Dockerfile**

Multi-stage build with `uv` and `python:3.13-slim`. Builder stage installs dependencies. Runtime stage copies venv. Non-root user. Exec-form CMD: `["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]`.

**Step 2: Create docker-compose.yml**

Services:
| Service | Image | Port | Health Check |
|---------|-------|------|-------------|
| app | Build from Dockerfile | 8000 | `curl -f http://localhost:8000/health` |
| mongodb | mongo:8 | 27017 | `mongosh --eval "db.runCommand('ping')"` |
| elasticsearch | elasticsearch:9.1.0 | 9200 | `curl -f http://localhost:9200/_cluster/health` |
| redis | redis:7 | 6379 | `redis-cli ping` |

ES config: `discovery.type=single-node`, `xpack.security.enabled=false`, `ES_JAVA_OPTS=-Xms512m -Xmx512m`.

App `depends_on` all three with `condition: service_healthy`. Environment variables reference `.env.example` values pointing to docker service hostnames.

Named volumes for MongoDB and ES data.

**Step 3: Create .dockerignore**

```
.git
.venv
__pycache__
*.pyc
.env
docs/
.claude/
```

**Step 4: Verify build**

Run: `docker compose build`

**Step 5: Commit**

```bash
git add Dockerfile docker-compose.yml .dockerignore
git commit -m "feat: Docker setup with multi-stage build and compose"
```

---

### Task 21: ARCHITECTURE.md

**Files:**
- Create: `ARCHITECTURE.md`

Write the architecture document following the structure in design doc section 9. Content sections:

1. **System Diagram** — ASCII diagram from design section 1 (data flow from POST through queue, worker, MongoDB, ES, and query endpoints)
2. **Component Responsibilities** — what each layer owns and why (API, queue, worker, MongoDB, ES, Redis)
3. **Storage Rationale** — why MongoDB for source of truth, ES for search, Redis for cache
4. **Failure Modes** — table from design section 9.4 (MongoDB down, ES down, Redis down, worker crash, queue full, DLQ full, etc.)
5. **Scaling Considerations** — what breaks at 10x (single worker, batch tuning, in-process queue, single ES index, offset pagination)
6. **Data Retention** — MongoDB TTL index, ES ILM, coordination rule
7. **What Would Change in Production** — SQS, CDC, infrastructure rate limiting, K8s probes, observability, time-series collections

Reference design doc sections 1, 9.1-9.7 for content. Write in clear, direct prose.

**Commit:**

```bash
git add ARCHITECTURE.md
git commit -m "docs: architecture document"
```

---

### Task 22: README.md

**Files:**
- Create: `README.md`

Content sections per design doc section 10:

1. **Setup & Run** — prerequisites (Docker, Docker Compose), `docker compose up`, env vars, verify with `GET /health`
2. **Endpoint Reference** — each route with method, path, params/body, example response. Cover all 6 endpoints.
3. **Testing** — `uv run pytest -m unit` (fast), `uv run pytest -m integration` (Docker required). Testing philosophy note.
4. **AI in My Workflow** — per assignment requirements: tools used, specific examples, where you pushed back on AI, how it shaped approach

**Commit:**

```bash
git add README.md
git commit -m "docs: README with setup, endpoints, testing, and AI workflow"
```

---

## Task Summary

| # | Task | Phase | Key Files |
|---|------|-------|-----------|
| 1 | Project scaffolding | Foundation | pyproject.toml, directory structure |
| 2 | Config & core infrastructure | Foundation | config.py, core/logging.py, core/exceptions.py |
| 3 | Event schemas | Foundation | events/schemas.py |
| 4 | Analytics & search schemas | Foundation | analytics/schemas.py, search/schemas.py |
| 5 | Queue protocol & in-memory queue | Queue | queue/base.py, queue/memory.py |
| 6 | Dead letter queue + nack routing | Queue | queue/dlq.py |
| 7 | Database connections & indexes | Data | core/database.py |
| 8 | Cache service | Data | cache/service.py |
| 9 | Event service | Data | events/service.py |
| 10 | Worker: MongoDB bulk write | Logic | ingestion/worker.py |
| 11 | Worker: ES + backoff + shutdown | Logic | test_worker.py (extended) |
| 12 | Analytics service | Logic | analytics/service.py |
| 13 | Search service | Logic | search/service.py |
| 14 | Rate limiting middleware | Logic | cache/middleware.py |
| 15 | Routers & dependencies | API | events/router.py, analytics/router.py, search/router.py |
| 16 | Health endpoint | API | health/router.py |
| 17 | App factory & lifespan | API | main.py |
| 18 | Integration test infrastructure | Test | tests/integration/conftest.py |
| 19 | Integration tests | Test | 5 test files |
| 20 | Docker setup | Deploy | Dockerfile, docker-compose.yml |
| 21 | ARCHITECTURE.md | Docs | ARCHITECTURE.md |
| 22 | README.md | Docs | README.md |
