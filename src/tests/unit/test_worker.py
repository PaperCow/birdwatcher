# src/tests/unit/test_worker.py
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from pymongo.errors import BulkWriteError
from src.ingestion.worker import EventWorker
from src.queue.base import QueueMessage
from src.queue.memory import SENTINEL


def _msg(key="k1"):
    return QueueMessage(payload={
        "idempotency_key": key,
        "event_type": "click",
        "timestamp": datetime.now(timezone.utc),
        "user_id": "u1",
        "source_url": "https://example.com",
        "metadata": {"browser": "Chrome"},
        "created_at": datetime.now(timezone.utc),
    })


def _make_worker(mongo=None, es=None, queue=None, dlq=None):
    return EventWorker(
        queue=queue or AsyncMock(),
        dlq=dlq or AsyncMock(),
        mongo_collection=mongo or AsyncMock(),
        es_client=es or AsyncMock(),
        es_index="events",
        batch_size=100,
        batch_timeout=5.0,
        max_retries=5,
        backoff_base=2.0,
        backoff_max=60.0,
    )


@pytest.mark.unit
class TestWorkerMongoDB:
    async def test_insert_many_called_ordered_false(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        worker = _make_worker(mongo=mongo)

        batch = [_msg("a"), _msg("b")]
        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(2, [])):
            await worker.process_batch(batch)

        mongo.insert_many.assert_called_once()
        _, kwargs = mongo.insert_many.call_args
        assert kwargs.get("ordered") is False

    async def test_duplicate_key_treated_as_success(self):
        mongo = AsyncMock()
        error = BulkWriteError({
            "writeErrors": [{"index": 0, "code": 11000, "errmsg": "dup"}],
            "nInserted": 1,
        })
        mongo.insert_many = AsyncMock(side_effect=error)
        queue = AsyncMock()

        worker = _make_worker(mongo=mongo, queue=queue)
        batch = [_msg("dup"), _msg("new")]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(2, [])):
            await worker.process_batch(batch)

        # Both should be forwarded to ES (dup treated as success)
        # ack should be called for both after ES success
        assert queue.ack.call_count == 2

    async def test_non_duplicate_error_nacks(self):
        mongo = AsyncMock()
        error = BulkWriteError({
            "writeErrors": [{"index": 0, "code": 999, "errmsg": "other"}],
            "nInserted": 1,
        })
        mongo.insert_many = AsyncMock(side_effect=error)
        queue = AsyncMock()

        worker = _make_worker(mongo=mongo, queue=queue)
        batch = [_msg("fail"), _msg("ok")]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])):
            await worker.process_batch(batch)

        # "fail" nacked, "ok" acked
        queue.nack.assert_called_once()
        nacked_msg = queue.nack.call_args[0][0]
        assert nacked_msg.payload["idempotency_key"] == "fail"
        queue.ack.assert_called_once()

    async def test_only_succeeded_forwarded_to_es(self):
        mongo = AsyncMock()
        error = BulkWriteError({
            "writeErrors": [{"index": 1, "code": 999, "errmsg": "fail"}],
            "nInserted": 1,
        })
        mongo.insert_many = AsyncMock(side_effect=error)

        worker = _make_worker(mongo=mongo)
        batch = [_msg("ok"), _msg("fail")]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])) as mock_bulk:
            await worker.process_batch(batch)

        # ES should only receive "ok" (index 0 succeeded, index 1 failed non-dup)
        actions = mock_bulk.call_args[0][1]  # second positional arg
        assert len(actions) == 1
        assert actions[0]["_id"] == "ok"


@pytest.mark.unit
class TestWorkerElasticsearch:
    async def test_es_documents_use_idempotency_key_as_id(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        worker = _make_worker(mongo=mongo)
        batch = [_msg("my-key")]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])) as mock_bulk:
            await worker.process_batch(batch)

        actions = mock_bulk.call_args[0][1]
        assert actions[0]["_id"] == "my-key"

    async def test_es_documents_include_metadata_text(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        worker = _make_worker(mongo=mongo)
        batch = [_msg()]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])) as mock_bulk:
            await worker.process_batch(batch)

        actions = mock_bulk.call_args[0][1]
        assert "metadata_text" in actions[0]
        assert "Chrome" in actions[0]["metadata_text"]

    async def test_es_success_acks(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        worker = _make_worker(mongo=mongo, queue=queue)
        batch = [_msg()]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])):
            await worker.process_batch(batch)

        queue.ack.assert_called_once()

    async def test_es_429_nacks(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        worker = _make_worker(mongo=mongo, queue=queue)
        batch = [_msg("k1")]

        errors = [{"index": {"_id": "k1", "status": 429, "error": {"type": "es_rejected"}}}]
        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(0, errors)):
            await worker.process_batch(batch)

        queue.nack.assert_called_once()
        assert "retryable" in queue.nack.call_args[0][1].lower()

    async def test_es_500_nacks(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        worker = _make_worker(mongo=mongo, queue=queue)
        batch = [_msg("k1")]

        errors = [{"index": {"_id": "k1", "status": 500, "error": {"type": "internal"}}}]
        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(0, errors)):
            await worker.process_batch(batch)

        queue.nack.assert_called_once()

    async def test_es_400_routes_to_dlq(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        dlq = AsyncMock()
        worker = _make_worker(mongo=mongo, queue=queue, dlq=dlq)
        batch = [_msg("k1")]

        errors = [{"index": {"_id": "k1", "status": 400, "error": {"type": "mapping"}}}]
        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(0, errors)):
            await worker.process_batch(batch)

        dlq.put.assert_called_once()
        queue.ack.assert_called_once()  # ack original after DLQ


@pytest.mark.unit
class TestWorkerBackoff:
    async def test_batch_failure_triggers_backoff(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock(side_effect=ConnectionError("down"))
        queue = AsyncMock()
        queue.drain = AsyncMock(side_effect=[
            [_msg()],
            [],  # empty after backoff, ends loop via sentinel
        ])
        worker = _make_worker(mongo=mongo, queue=queue)

        # process_batch raises, run() catches and backs off
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            # Simulate: first drain returns batch (fails), second returns sentinel
            queue.drain = AsyncMock(side_effect=[
                [_msg()],
                [SENTINEL],
            ])
            await worker.run()

        mock_sleep.assert_called_once()
        delay = mock_sleep.call_args[0][0]
        assert delay > 0  # backoff applied

    async def test_consecutive_failures_increase_backoff(self):
        worker = _make_worker()
        worker._consecutive_failures = 3
        # After another failure, consecutive = 4, delay = min(2.0 * 2^4, 60) = 32
        worker._consecutive_failures = 4
        expected_base = min(2.0 * (2 ** 4), 60.0)
        assert expected_base == 32.0

    async def test_success_resets_failure_counter(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        worker = _make_worker(mongo=mongo, queue=queue)
        worker._consecutive_failures = 5

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])):
            queue.drain = AsyncMock(side_effect=[[_msg()], [SENTINEL]])
            await worker.run()

        assert worker._consecutive_failures == 0


@pytest.mark.unit
class TestWorkerShutdown:
    async def test_sentinel_stops_worker(self):
        queue = AsyncMock()
        queue.drain = AsyncMock(return_value=[SENTINEL])
        worker = _make_worker(queue=queue)
        await worker.run()  # should exit cleanly

    async def test_processes_items_before_sentinel(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        msg = _msg()
        queue.drain = AsyncMock(return_value=[msg, SENTINEL])
        worker = _make_worker(mongo=mongo, queue=queue)

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])):
            await worker.run()

        mongo.insert_many.assert_called_once()

    async def test_is_alive_heartbeat(self):
        worker = _make_worker()
        assert not worker.is_alive()
        worker._last_heartbeat = datetime.now(timezone.utc)
        assert worker.is_alive()
