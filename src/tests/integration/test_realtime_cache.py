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
