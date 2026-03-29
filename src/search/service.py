from __future__ import annotations

from datetime import datetime
from typing import Any

from src.search.schemas import SearchQuery


def extract_metadata_text(metadata: dict) -> str:
    """Recursively extract all scalar values from metadata into a single text string."""
    values: list[str] = []

    def _walk(obj: object) -> None:
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


class SearchService:
    """Full-text search over event metadata via Elasticsearch."""

    def __init__(self, es_client: Any, index: str) -> None:
        self._es = es_client
        self._index = index

    def build_query(
        self,
        q: str,
        event_type: str | None = None,
        user_id: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> dict[str, Any]:
        """Build an Elasticsearch bool query with match + optional term/range filters."""
        must = [{"match": {"metadata_text": q}}]
        filters: list[dict[str, Any]] = []

        if event_type is not None:
            filters.append({"term": {"event_type": event_type}})
        if user_id is not None:
            filters.append({"term": {"user_id": user_id}})

        if start_date is not None or end_date is not None:
            range_clause: dict[str, Any] = {}
            if start_date is not None:
                range_clause["gte"] = start_date
            if end_date is not None:
                range_clause["lte"] = end_date
            filters.append({"range": {"timestamp": range_clause}})

        query: dict[str, Any] = {"query": {"bool": {"must": must}}}
        if filters:
            query["query"]["bool"]["filter"] = filters
        return query

    async def search(self, query: SearchQuery) -> dict[str, Any]:
        """Execute a search against Elasticsearch and return transformed results."""
        es_query = self.build_query(
            q=query.q,
            event_type=query.event_type,
            user_id=query.user_id,
            start_date=query.start_date,
            end_date=query.end_date,
        )

        # elasticsearch-py 9.x: use keyword arguments directly
        response = await self._es.search(
            index=self._index,
            query=es_query["query"],
            from_=query.skip,
            size=query.limit,
        )

        raw_hits = response["hits"]["hits"]
        total = response["hits"]["total"]["value"]

        hits = []
        for hit in raw_hits:
            source = hit["_source"]
            hits.append({
                "id": hit["_id"],
                "event_type": source["event_type"],
                "timestamp": source["timestamp"],
                "user_id": source["user_id"],
                "source_url": source["source_url"],
                "metadata": source.get("metadata"),
                "score": hit["_score"],
            })

        return {"hits": hits, "total": total}
