# Module Reorganization Design

## Problem

The `src/cache/` module bundles two unrelated concerns: a general-purpose Redis
cache service and ASGI rate-limiting middleware. Additionally,
`extract_metadata_text()` lives in `src/search/service.py` but is imported by
`src/ingestion/worker.py`, creating a cross-domain dependency that doesn't
reflect the function's actual role as a shared utility.

## Changes

### 1. Move CacheService to core

`src/cache/service.py` → `src/core/cache.py`

CacheService is a thin Redis get/set wrapper with JSON serialization — the same
infrastructure layer as `database.py` and `logging.py`. Moving it to `core/`
reflects its role as foundational infrastructure, not a domain feature.

### 2. Move RateLimitMiddleware to middleware

`src/cache/middleware.py` → `src/middleware/rate_limit.py`

Rate limiting is an HTTP-layer concern (ASGI middleware), not a caching concern.
A dedicated `middleware/` module gives it a clear home and provides a natural
location for future middleware (request logging, auth, etc.).

### 3. Extract extract_metadata_text to core

`extract_metadata_text()` from `src/search/service.py` → `src/core/text.py`

This pure function recursively extracts scalar values from nested dicts. It's
used by both `ingestion/worker.py` (to build ES documents) and conceptually
belongs to search indexing, but neither domain owns it. Placing it in `core/`
breaks the `ingestion → search` cross-dependency.

### 4. Delete src/cache/

Empty after moves. Remove the directory and its `__init__.py`.

## Import Updates

| File | Old import | New import |
|---|---|---|
| `src/main.py` | `from src.cache.middleware import RateLimitMiddleware` | `from src.middleware.rate_limit import RateLimitMiddleware` |
| `src/main.py` | `from src.cache.service import CacheService` | `from src.core.cache import CacheService` |
| `src/ingestion/worker.py` | `from src.search.service import extract_metadata_text` | `from src.core.text import extract_metadata_text` |
| `src/tests/unit/test_rate_limiter.py` | `from src.cache.middleware import RateLimitMiddleware` | `from src.middleware.rate_limit import RateLimitMiddleware` |
| `src/tests/unit/test_cache_service.py` | `from src.cache.service import CacheService` | `from src.core.cache import CacheService` |

## Result Structure

```
src/
├── main.py
├── config.py
├── core/
│   ├── cache.py          ← from cache/service.py
│   ├── database.py
│   ├── exceptions.py
│   ├── logging.py
│   └── text.py           ← extracted from search/service.py
├── middleware/
│   └── rate_limit.py     ← from cache/middleware.py
├── queue/
│   ├── base.py
│   ├── dlq.py
│   └── memory.py
├── events/
│   ├── schemas.py
│   ├── service.py
│   ├── dependencies.py
│   └── router.py
├── search/
│   ├── schemas.py
│   ├── service.py        ← extract_metadata_text removed
│   └── router.py
├── analytics/
│   ├── schemas.py
│   ├── service.py
│   └── router.py
├── ingestion/
│   └── worker.py
└── health/
    └── router.py
```

## Scope

Pure structural refactor — no logic changes. All file contents are preserved
as-is; only import paths change.
