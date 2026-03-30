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
