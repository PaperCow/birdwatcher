# src/ingestion/worker.py
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone

from pymongo.errors import BulkWriteError
from elasticsearch.helpers import async_bulk

from src.queue.base import QueueMessage
from src.queue.memory import SENTINEL
from src.search.service import extract_metadata_text
from src.core.logging import get_logger

logger = get_logger(component="worker")


class EventWorker:
    def __init__(
        self,
        queue,
        dlq,
        mongo_collection,
        es_client,
        es_index: str,
        batch_size: int,
        batch_timeout: float,
        max_retries: int,
        backoff_base: float,
        backoff_max: float,
    ):
        self._queue = queue
        self._dlq = dlq
        self._mongo = mongo_collection
        self._es = es_client
        self._es_index = es_index
        self._batch_size = batch_size
        self._batch_timeout = batch_timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._consecutive_failures = 0
        self._last_heartbeat: datetime | None = None

    def is_alive(self, staleness_seconds: float = 30.0) -> bool:
        if self._last_heartbeat is None:
            return False
        elapsed = (datetime.now(timezone.utc) - self._last_heartbeat).total_seconds()
        return elapsed < staleness_seconds

    async def run(self) -> None:
        try:
            while True:
                batch = await self._queue.drain(self._batch_size, self._batch_timeout)
                self._last_heartbeat = datetime.now(timezone.utc)

                real_batch, has_sentinel = self._split_sentinel(batch)
                if real_batch:
                    try:
                        await self.process_batch(real_batch)
                        self._consecutive_failures = 0
                    except Exception as e:
                        logger.error("batch_level_failure", error=str(e))
                        for msg in real_batch:
                            await self._queue.nack(msg, str(e))
                        self._consecutive_failures += 1
                        delay = min(
                            self._backoff_base * (2 ** self._consecutive_failures),
                            self._backoff_max,
                        )
                        await asyncio.sleep(delay + random.uniform(0, delay * 0.1))
                if has_sentinel:
                    logger.info("shutdown_sentinel_received")
                    break
        except Exception:
            logger.exception("worker_fatal_error")

    def _split_sentinel(self, batch: list) -> tuple[list, bool]:
        for i, item in enumerate(batch):
            if item is SENTINEL:
                return batch[:i], True
        return batch, False

    async def process_batch(self, batch: list[QueueMessage]) -> None:
        # 1. MongoDB bulk write
        succeeded, failed = await self._write_mongodb(batch)

        # 2. Nack mongo failures
        for msg, error_msg in failed:
            await self._queue.nack(msg, error_msg)

        # 3. ES bulk write for succeeded items
        if succeeded:
            await self._write_elasticsearch(succeeded)

    async def _write_mongodb(
        self, batch: list[QueueMessage]
    ) -> tuple[list[QueueMessage], list[tuple[QueueMessage, str]]]:
        documents = [self._to_mongo_doc(m) for m in batch]
        succeeded = list(batch)
        failed: list[tuple[QueueMessage, str]] = []
        try:
            await self._mongo.insert_many(documents, ordered=False)
        except BulkWriteError as e:
            error_map: dict[int, int] = {}
            for err in e.details.get("writeErrors", []):
                error_map[err["index"]] = err["code"]
            succeeded = []
            failed = []
            for i, msg in enumerate(batch):
                if i not in error_map:
                    succeeded.append(msg)
                elif error_map[i] == 11000:
                    succeeded.append(msg)  # duplicate key = success
                else:
                    failed.append((msg, f"MongoDB error code {error_map[i]}"))
        return succeeded, failed

    async def _write_elasticsearch(self, batch: list[QueueMessage]) -> None:
        actions = [self._to_es_action(m) for m in batch]
        success_count, errors = await async_bulk(
            self._es,
            actions,
            raise_on_error=False,
            raise_on_exception=False,
        )
        # async_bulk returns Union[int, List] but is always List when stats_only=False (the default)
        assert isinstance(errors, list)
        failed_map: dict[str, int] = {}
        for err_item in errors:
            action_type = list(err_item.keys())[0]
            detail = err_item[action_type]
            failed_map[detail["_id"]] = detail["status"]

        for msg in batch:
            key = msg.payload["idempotency_key"]
            if key not in failed_map:
                await self._queue.ack(msg.message_id)
            else:
                status = failed_map[key]
                if status == 429 or status >= 500:
                    await self._queue.nack(msg, f"ES retryable status {status}")
                else:
                    msg.last_error = f"ES permanent status {status}"
                    await self._dlq.put(msg)
                    await self._queue.ack(msg.message_id)

    def _to_mongo_doc(self, msg: QueueMessage) -> dict:
        p = msg.payload
        return {
            "idempotency_key": p["idempotency_key"],
            "event_type": p["event_type"],
            "timestamp": p["timestamp"],
            "user_id": p["user_id"],
            "source_url": p["source_url"],
            "metadata": p.get("metadata"),
            "created_at": p["created_at"],
        }

    def _to_es_action(self, msg: QueueMessage) -> dict:
        p = msg.payload
        metadata = p.get("metadata") or {}
        return {
            "_index": self._es_index,
            "_id": p["idempotency_key"],
            "event_type": p["event_type"],
            "timestamp": p["timestamp"],
            "user_id": p["user_id"],
            "source_url": p["source_url"],
            "metadata": metadata,
            "metadata_text": extract_metadata_text(metadata),
        }
