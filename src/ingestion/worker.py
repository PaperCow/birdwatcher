# src/ingestion/worker.py
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone

from pymongo.errors import BulkWriteError
from elasticsearch.helpers import async_bulk

from src.queue.base import ReceivedMessage
from src.core.text import extract_metadata_text
from src.core.logging import get_logger

logger = get_logger(component="worker")


class EventWorker:
    def __init__(
        self,
        queue,
        mongo_collection,
        es_client,
        es_index: str,
        batch_size: int,
        batch_timeout: float,
        backoff_base: float,
        backoff_max: float,
        shutdown_event: asyncio.Event,
    ):
        self._queue = queue
        self._mongo = mongo_collection
        self._es = es_client
        self._es_index = es_index
        self._batch_size = batch_size
        self._batch_timeout = batch_timeout
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._shutdown_event = shutdown_event
        self._consecutive_failures = 0
        self._last_heartbeat: datetime | None = None

    def is_alive(self, staleness_seconds: float = 30.0) -> bool:
        if self._last_heartbeat is None:
            return False
        elapsed = (datetime.now(timezone.utc) - self._last_heartbeat).total_seconds()
        return elapsed < staleness_seconds

    async def run(self) -> None:
        while True:
            try:
                while not self._shutdown_event.is_set():
                    batch = await self._queue.drain(self._batch_size, self._batch_timeout)
                    self._last_heartbeat = datetime.now(timezone.utc)

                    if batch:
                        try:
                            await self.process_batch(batch)
                            self._consecutive_failures = 0
                        except Exception as e:
                            logger.error("batch_level_failure", error=str(e))
                            for received in batch:
                                await self._queue.nack(received.receipt_handle)
                            delay = min(
                                self._backoff_base * (2 ** self._consecutive_failures),
                                self._backoff_max,
                            )
                            self._consecutive_failures += 1
                            await asyncio.sleep(delay + random.uniform(0, delay * 0.1))
                logger.info("shutdown_event_received")
                return
            except Exception:
                logger.exception("worker_fatal_error")
                delay = min(
                    self._backoff_base * (2 ** self._consecutive_failures),
                    self._backoff_max,
                )
                self._consecutive_failures += 1
                await asyncio.sleep(delay + random.uniform(0, delay * 0.1))
                logger.info("worker_restarting")

    async def process_batch(self, batch: list[ReceivedMessage]) -> None:
        # 1. MongoDB bulk write
        succeeded, failed = await self._write_mongodb(batch)

        # 2. Nack mongo failures
        for received, error_msg in failed:
            logger.warning("mongodb_item_failed", receipt_handle=received.receipt_handle, error=error_msg)
            await self._queue.nack(received.receipt_handle)

        # 3. ES bulk write for succeeded items
        if succeeded:
            await self._write_elasticsearch(succeeded)

    async def _write_mongodb(
        self, batch: list[ReceivedMessage]
    ) -> tuple[list[ReceivedMessage], list[tuple[ReceivedMessage, str]]]:
        documents = [self._to_mongo_doc(r) for r in batch]
        succeeded = list(batch)
        failed: list[tuple[ReceivedMessage, str]] = []
        try:
            await self._mongo.insert_many(documents, ordered=False)
        except BulkWriteError as e:
            error_map: dict[int, int] = {}
            for err in e.details.get("writeErrors", []):
                error_map[err["index"]] = err["code"]
            succeeded = []
            failed = []
            for i, received in enumerate(batch):
                if i not in error_map:
                    succeeded.append(received)
                elif error_map[i] == 11000:
                    succeeded.append(received)
                else:
                    failed.append((received, f"MongoDB error code {error_map[i]}"))
        return succeeded, failed

    async def _write_elasticsearch(self, batch: list[ReceivedMessage]) -> None:
        actions = [self._to_es_action(r) for r in batch]
        success_count, errors = await async_bulk(
            self._es,
            actions,
            raise_on_error=False,
            raise_on_exception=False,
        )
        if not isinstance(errors, list):
            raise TypeError(f"Expected error list from async_bulk, got {type(errors).__name__}")
        failed_map: dict[str, int] = {}
        for err_item in errors:
            action_type = next(iter(err_item))
            detail = err_item[action_type]
            failed_map[detail["_id"]] = detail["status"]

        for received in batch:
            key = received.queue_message.payload["idempotency_key"]
            if key not in failed_map:
                await self._queue.ack(received.receipt_handle)
            else:
                status = failed_map[key]
                if status == 429 or status >= 500:
                    logger.warning("es_retryable_failure", receipt_handle=received.receipt_handle, status=status)
                    await self._queue.nack(received.receipt_handle)
                else:
                    # Permanent failure: ack to remove from queue, log for observability.
                    # The message will never succeed — retrying is pointless.
                    logger.error(
                        "es_permanent_failure",
                        receipt_handle=received.receipt_handle,
                        idempotency_key=key,
                        status=status,
                    )
                    await self._queue.ack(received.receipt_handle)

    def _to_mongo_doc(self, received: ReceivedMessage) -> dict:
        p = received.queue_message.payload
        return {
            "idempotency_key": p["idempotency_key"],
            "event_type": p["event_type"],
            "timestamp": p["timestamp"],
            "user_id": p["user_id"],
            "source_url": p["source_url"],
            "metadata": p.get("metadata"),
            "created_at": p["created_at"],
        }

    def _to_es_action(self, received: ReceivedMessage) -> dict:
        p = received.queue_message.payload
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
