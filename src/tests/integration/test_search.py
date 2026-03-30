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
