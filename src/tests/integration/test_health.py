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
