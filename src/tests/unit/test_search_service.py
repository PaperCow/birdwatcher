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

    def test_with_user_id_filter(self):
        service = SearchService(es_client=AsyncMock(), index="events")
        body = service.build_query(q="test", user_id="user-42")
        filters = body["query"]["bool"]["filter"]
        assert any(f.get("term", {}).get("user_id") == "user-42" for f in filters)

    def test_with_end_date_only(self):
        service = SearchService(es_client=AsyncMock(), index="events")
        now = datetime.now(timezone.utc)
        body = service.build_query(q="test", end_date=now)
        filters = body["query"]["bool"]["filter"]
        range_filter = next(f for f in filters if "range" in f)
        assert range_filter["range"]["timestamp"]["lte"] == now

    def test_with_full_date_range(self):
        service = SearchService(es_client=AsyncMock(), index="events")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 3, 1, tzinfo=timezone.utc)
        body = service.build_query(q="test", start_date=start, end_date=end)
        filters = body["query"]["bool"]["filter"]
        range_filter = next(f for f in filters if "range" in f)
        assert range_filter["range"]["timestamp"]["gte"] == start
        assert range_filter["range"]["timestamp"]["lte"] == end

    def test_no_filters_when_none(self):
        service = SearchService(es_client=AsyncMock(), index="events")
        body = service.build_query(q="test")
        filters = body["query"]["bool"].get("filter", [])
        assert filters == []

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
