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
