from __future__ import annotations

from typing import Awaitable, cast

import redis.asyncio as aioredis
from elasticsearch import AsyncElasticsearch, BadRequestError
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from src.config import Settings
from src.core.logging import get_logger

logger = get_logger(component="database")

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


class DatabaseManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.mongo_client: AsyncMongoClient | None = None
        self.mongo_db: AsyncDatabase | None = None
        self.es_client: AsyncElasticsearch | None = None
        self.redis_client: aioredis.Redis | None = None

    async def connect(self) -> None:
        self.mongo_client = AsyncMongoClient(self.settings.mongodb_url)
        self.mongo_db = self.mongo_client[self.settings.mongodb_database]

        self.es_client = AsyncElasticsearch(self.settings.elasticsearch_url)

        self.redis_client = aioredis.Redis.from_url(
            self.settings.redis_url,
            max_connections=self.settings.redis_max_connections,
            decode_responses=True,
        )

    async def create_indexes(self) -> None:
        if self.mongo_db is None or self.es_client is None:
            raise RuntimeError("DatabaseManager is not connected")

        collection = self.mongo_db["events"]

        # MongoDB indexes (idempotent)
        await collection.create_index("idempotency_key", unique=True)
        await collection.create_index([("event_type", 1), ("timestamp", 1)])
        await collection.create_index("user_id")
        await collection.create_index("source_url")
        await collection.create_index("timestamp")
        logger.info("mongodb_indexes_created")

        # ES index with explicit mapping (ignore if already exists)
        try:
            await self.es_client.indices.create(
                index=self.settings.elasticsearch_index,
                mappings=ES_MAPPING,
            )
        except BadRequestError:
            pass
        logger.info(
            "elasticsearch_index_created",
            index=self.settings.elasticsearch_index,
        )

    async def close(self) -> None:
        if self.mongo_client:
            await self.mongo_client.close()
        if self.es_client:
            await self.es_client.close()
        if self.redis_client:
            await self.redis_client.aclose()

    async def check_mongodb(self) -> bool:
        if self.mongo_client is None:
            return False
        try:
            await self.mongo_client.admin.command("ping")
            return True
        except Exception:
            return False

    async def check_elasticsearch(self) -> bool:
        if self.es_client is None:
            return False
        try:
            return await self.es_client.ping()
        except Exception:
            return False

    async def check_redis(self) -> bool:
        if self.redis_client is None:
            return False
        try:
            # redis typestub returns Union[Awaitable[bool], bool]; async client always returns Awaitable
            return await cast(Awaitable[bool], self.redis_client.ping())
        except Exception:
            return False
