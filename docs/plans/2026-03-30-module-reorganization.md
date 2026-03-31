# Module Reorganization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reorganize `src/cache/` into proper locations — CacheService to `core/`, rate limiter to `middleware/`, shared text utility to `core/` — and delete the `cache/` module.

**Architecture:** Pure structural refactor. Move files, update imports, delete the old module. No logic changes. Existing tests validate correctness after import paths are updated.

**Tech Stack:** Python, FastAPI, pytest

---

### Task 1: Move CacheService to core

**Files:**
- Create: `src/core/cache.py`
- Delete: `src/cache/service.py` (after task 4)
- Modify: `src/main.py:12`
- Modify: `src/tests/unit/test_cache_service.py:4`

**Step 1: Copy cache service to new location**

Copy `src/cache/service.py` to `src/core/cache.py`. Contents are identical — no changes to the file itself.

**Step 2: Update import in main.py**

In `src/main.py` line 12, change:
```python
from src.cache.service import CacheService
```
to:
```python
from src.core.cache import CacheService
```

**Step 3: Update import in test**

In `src/tests/unit/test_cache_service.py` line 4, change:
```python
from src.cache.service import CacheService
```
to:
```python
from src.core.cache import CacheService
```

**Step 4: Run cache tests**

Run: `pytest src/tests/unit/test_cache_service.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/core/cache.py src/main.py src/tests/unit/test_cache_service.py
git commit -m "refactor: move CacheService to core"
```

---

### Task 2: Move RateLimitMiddleware to middleware module

**Files:**
- Create: `src/middleware/__init__.py`
- Create: `src/middleware/rate_limit.py`
- Delete: `src/cache/middleware.py` (after task 4)
- Modify: `src/main.py:11`
- Modify: `src/tests/unit/test_rate_limiter.py:9`

**Step 1: Create middleware module and copy file**

Create `src/middleware/__init__.py` (empty). Copy `src/cache/middleware.py` to `src/middleware/rate_limit.py`. Contents are identical.

**Step 2: Update import in main.py**

In `src/main.py` line 11, change:
```python
from src.cache.middleware import RateLimitMiddleware
```
to:
```python
from src.middleware.rate_limit import RateLimitMiddleware
```

**Step 3: Update import in test**

In `src/tests/unit/test_rate_limiter.py` line 9, change:
```python
from src.cache.middleware import RateLimitMiddleware
```
to:
```python
from src.middleware.rate_limit import RateLimitMiddleware
```

**Step 4: Run rate limiter tests**

Run: `pytest src/tests/unit/test_rate_limiter.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/middleware/ src/main.py src/tests/unit/test_rate_limiter.py
git commit -m "refactor: move RateLimitMiddleware to middleware module"
```

---

### Task 3: Extract extract_metadata_text to core/text.py

**Files:**
- Create: `src/core/text.py`
- Modify: `src/search/service.py` (remove function, lines 9-29)
- Modify: `src/ingestion/worker.py:12`

**Step 1: Create core/text.py with the extracted function**

```python
def extract_metadata_text(metadata: dict) -> str:
    """Recursively extract all scalar values from metadata into a single text string.

    Leaf values only — keys are queryable via the ES flattened metadata field.
    """
    values: list[str] = []

    def _walk(obj: object) -> None:
        if isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)
        elif obj is None or isinstance(obj, bool):
            return
        else:
            values.append(str(obj))

    _walk(metadata)
    return " ".join(values)
```

**Step 2: Remove extract_metadata_text from search/service.py**

In `src/search/service.py`, remove the function definition (lines 9-29, the `extract_metadata_text` function and the blank line after it). The file should go from the imports directly to `class SearchService:`.

**Step 3: Update import in worker.py**

In `src/ingestion/worker.py` line 12, change:
```python
from src.search.service import extract_metadata_text
```
to:
```python
from src.core.text import extract_metadata_text
```

**Step 4: Run all tests**

Run: `pytest src/tests/unit/ -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/core/text.py src/search/service.py src/ingestion/worker.py
git commit -m "refactor: extract extract_metadata_text to core/text"
```

---

### Task 4: Delete src/cache/ module

**Files:**
- Delete: `src/cache/service.py`
- Delete: `src/cache/middleware.py`
- Delete: `src/cache/__init__.py`
- Delete: `src/cache/` directory

**Step 1: Delete the cache module**

```bash
rm -rf src/cache/
```

**Step 2: Run full test suite**

Run: `pytest src/tests/ -v`
Expected: All tests PASS — no remaining imports from `src.cache`

**Step 3: Verify no stale references**

```bash
grep -r "from src.cache" src/
```
Expected: No matches (docs/ references are fine, they're historical)

**Step 4: Commit**

```bash
git add -u src/cache/
git commit -m "refactor: remove empty cache module"
```
