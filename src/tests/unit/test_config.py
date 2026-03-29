import pytest
from src.config import Settings


@pytest.mark.unit
class TestSettings:
    def test_defaults_load(self):
        settings = Settings()
        assert settings.mongodb_url == "mongodb://localhost:27017"
        assert settings.mongodb_database == "birdwatcher"
        assert settings.elasticsearch_url == "http://localhost:9200"
        assert settings.elasticsearch_index == "events"
        assert settings.redis_url == "redis://localhost:6379"

    def test_queue_defaults(self):
        settings = Settings()
        assert settings.queue_max_size == 10000
        assert settings.dlq_max_size == 1000
        assert settings.batch_size == 100
        assert settings.batch_timeout == 5.0
        assert settings.max_retries == 5

    def test_validation_defaults(self):
        settings = Settings()
        assert settings.max_metadata_size == 65536
        assert settings.max_metadata_depth == 10
        assert settings.timestamp_past_window_days == 30
        assert settings.timestamp_future_window_minutes == 5

    def test_stats_limits(self):
        settings = Settings()
        assert settings.hourly_max_days == 7
        assert settings.daily_max_days == 365
        assert settings.weekly_max_days == 730

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("APP_MONGODB_DATABASE", "custom_db")
        settings = Settings()
        assert settings.mongodb_database == "custom_db"
