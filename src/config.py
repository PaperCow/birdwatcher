from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_")

    # MongoDB
    mongodb_url: str = "mongodb://localhost:27017"
    mongodb_database: str = "birdwatcher"

    # Elasticsearch
    elasticsearch_url: str = "http://localhost:9200"
    elasticsearch_index: str = "events"

    # Redis
    redis_url: str = "redis://localhost:6379"
    redis_max_connections: int = 20

    # Queue
    queue_max_size: int = 10000
    dlq_max_size: int = 1000

    # Worker
    batch_size: int = 100
    batch_timeout: float = 5.0
    max_retries: int = 5

    # Cache
    realtime_stats_ttl: int = 30

    # Rate limiting
    rate_limit_requests: int = 100
    rate_limit_window: int = 60

    # Validation
    max_metadata_size: int = 65536
    max_metadata_depth: int = 10
    timestamp_past_window_days: int = 30
    timestamp_future_window_minutes: int = 5

    # Stats query limits
    hourly_max_days: int = 7
    daily_max_days: int = 365
    weekly_max_days: int = 730

    # Backoff
    backoff_base: float = 2.0
    backoff_max: float = 60.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
