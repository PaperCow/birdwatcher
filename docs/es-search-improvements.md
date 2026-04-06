# Elasticsearch Full-Text Search Improvements

This document describes two gaps in the current full-text search implementation
and the specific code changes needed to address them.

## Current Behavior

The search endpoint (`GET /events/search`) matches the user's query against a
single field: `metadata_text`. This field is built by `extract_metadata_text()`,
which recursively walks the `metadata` dict and concatenates all scalar **values**
into one string, analyzed with the `standard` analyzer.

Fields outside of metadata — `source_url`, `event_type`, `user_id` — are mapped
as `keyword` and are only available for exact-match filtering, not full-text
search.

## Gap 1: `source_url` Is Not Searchable

`source_url` is mapped as `keyword`, so a search for "example" will not match an
event whose source URL is `https://example.com/birds/robin`. Users must know the
exact URL to filter by it.

### Fix

Add a `text` sub-field to `source_url` using the `simple` analyzer, and include
it in the search query via `multi_match`.

**Why `simple` and not `standard`?** The `standard` analyzer uses Unicode text
segmentation, which keeps domain names intact as single tokens (e.g.,
`example.com`). A search for "example" would not match. The `simple` analyzer
splits on any non-letter character, producing individual tokens for each URL
component:

| Analyzer | Input | Tokens |
|---|---|---|
| `standard` | `https://example.com/birds/robin` | `https`, `example.com`, `birds`, `robin` |
| `simple` | `https://example.com/birds/robin` | `https`, `example`, `com`, `birds`, `robin` |

The `simple` analyzer also lowercases, which is appropriate since URLs are
case-insensitive in the domain portion and typically lowercase in paths.

## Gap 2: Metadata Keys Are Not Searchable

`extract_metadata_text()` extracts only leaf **values**, not keys. Given metadata
like `{"species": "robin", "location": "central park"}`, the indexed text is
`"robin central park"`. A search for "species" or "location" returns nothing.

This matters because users often search by field name when they don't remember
the exact value, or when they want to find all events that contain a particular
kind of metadata.

### Fix

Include dict keys in the text extraction alongside their values.

## Code Changes

### 1. `src/core/database.py` — Add text sub-field to source_url

```python
# before
ES_MAPPING = {
    "properties": {
        "event_type": {"type": "keyword"},
        "timestamp": {"type": "date"},
        "user_id": {"type": "keyword"},
        "source_url": {"type": "keyword"},
        "metadata": {"type": "flattened"},
        "metadata_text": {"type": "text", "analyzer": "standard"},
    }
}

# after
ES_MAPPING = {
    "properties": {
        "event_type": {"type": "keyword"},
        "timestamp": {"type": "date"},
        "user_id": {"type": "keyword"},
        "source_url": {
            "type": "keyword",
            "fields": {
                "text": {"type": "text", "analyzer": "simple"}
            },
        },
        "metadata": {"type": "flattened"},
        "metadata_text": {"type": "text", "analyzer": "standard"},
    }
}
```

The `keyword` type is preserved for exact-match filtering. The `.text` sub-field
adds a second representation of the same data, tokenized for full-text search.

### 2. `src/core/text.py` — Include metadata keys in extraction

```python
# before
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

# after
def _walk(obj: object) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            values.append(str(k))
            _walk(v)
    elif isinstance(obj, list):
        for item in obj:
            _walk(item)
    elif obj is None or isinstance(obj, bool):
        return
    else:
        values.append(str(obj))
```

The docstring should also be updated to reflect that keys are now included:

```python
def extract_metadata_text(metadata: dict) -> str:
    """Recursively extract all keys and scalar values from metadata into a single text string."""
```

### 3. `src/search/service.py` — Search both fields with multi_match

```python
# before
must = [{"match": {"metadata_text": q}}]

# after
must = [{"multi_match": {"query": q, "fields": ["metadata_text", "source_url.text"]}}]
```

This searches both fields and returns the best-matching score. Boosting can be
added later if one field should be weighted higher (e.g.,
`"metadata_text^2", "source_url.text"`).

## Deployment Considerations

### New deployments

No special handling. The updated mapping is applied at index creation during
application startup.

### Existing deployments

The ES mapping change and metadata key extraction only apply to newly indexed
documents. Existing documents will be missing:

- The `source_url.text` sub-field tokens
- Metadata keys in `metadata_text`

To backfill:

1. **Update the mapping** using the
   [Put Mapping API](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/put-mapping)
   to add the `source_url.text` sub-field (ES allows adding new sub-fields to
   existing fields without reindexing the whole index).

2. **Reindex in place** to regenerate `source_url.text` tokens from the existing
   `source_url` keyword values:

   ```
   POST /events/_update_by_query?refresh=true
   {
     "query": {"match_all": {}},
     "script": {
       "source": "ctx._source.source_url = ctx._source.source_url",
       "lang": "painless"
     }
   }
   ```

   This re-triggers analysis on the `source_url` field, populating the `.text`
   sub-field.

3. **Re-ingest from MongoDB** to regenerate `metadata_text` with keys included.
   The `_update_by_query` approach does not work here because the key extraction
   happens in Python (`extract_metadata_text`), not in an ES analyzer. A script
   that reads events from MongoDB and re-indexes them through the normal
   ingestion path is the cleanest option.

### Test changes

Existing tests for `extract_metadata_text` will need updating — they currently
assert that only values appear in the output. The search integration tests should
also be extended to verify that:

- Searching for a metadata key (e.g., "species") returns matching events
- Searching for a URL path segment (e.g., "birds") returns events with that
  segment in `source_url`
