# src/tests/unit/test_worker.py
import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from pymongo.errors import BulkWriteError
from src.ingestion.worker import EventWorker
from src.queue.base import QueueMessage, ReceivedMessage


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


def _received(key="k1"):
    msg = _msg(key)
    return ReceivedMessage(queue_message=msg, receipt_handle=msg.message_id)


def _make_worker(mongo=None, es=None, queue=None, shutdown_event=None):
    return EventWorker(
        queue=queue or AsyncMock(),
        mongo_collection=mongo or AsyncMock(),
        es_client=es or AsyncMock(),
        es_index="events",
        batch_size=100,
        batch_timeout=5.0,
        backoff_base=2.0,
        backoff_max=60.0,
        shutdown_event=shutdown_event or asyncio.Event(),
    )


@pytest.mark.unit
class TestWorkerMongoDB:
    async def test_insert_many_called_ordered_false(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        worker = _make_worker(mongo=mongo)

        batch = [_received("a"), _received("b")]
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
        batch = [_received("dup"), _received("new")]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(2, [])):
            await worker.process_batch(batch)

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
        batch = [_received("fail"), _received("ok")]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])):
            await worker.process_batch(batch)

        queue.nack.assert_called_once()
        nacked_handle = queue.nack.call_args[0][0]
        assert nacked_handle == batch[0].receipt_handle
        queue.ack.assert_called_once()

    async def test_only_succeeded_forwarded_to_es(self):
        mongo = AsyncMock()
        error = BulkWriteError({
            "writeErrors": [{"index": 1, "code": 999, "errmsg": "fail"}],
            "nInserted": 1,
        })
        mongo.insert_many = AsyncMock(side_effect=error)

        worker = _make_worker(mongo=mongo)
        batch = [_received("ok"), _received("fail")]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])) as mock_bulk:
            await worker.process_batch(batch)

        actions = mock_bulk.call_args[0][1]
        assert len(actions) == 1
        assert actions[0]["_id"] == "ok"


@pytest.mark.unit
class TestWorkerElasticsearch:
    async def test_es_documents_use_idempotency_key_as_id(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        worker = _make_worker(mongo=mongo)
        batch = [_received("my-key")]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])) as mock_bulk:
            await worker.process_batch(batch)

        actions = mock_bulk.call_args[0][1]
        assert actions[0]["_id"] == "my-key"

    async def test_es_documents_include_metadata_text(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        worker = _make_worker(mongo=mongo)
        batch = [_received()]

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
        batch = [_received()]

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])):
            await worker.process_batch(batch)

        queue.ack.assert_called_once()

    async def test_es_429_nacks(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        worker = _make_worker(mongo=mongo, queue=queue)
        batch = [_received("k1")]

        errors = [{"index": {"_id": "k1", "status": 429, "error": {"type": "es_rejected"}}}]
        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(0, errors)):
            await worker.process_batch(batch)

        queue.nack.assert_called_once()

    async def test_es_500_nacks(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        worker = _make_worker(mongo=mongo, queue=queue)
        batch = [_received("k1")]

        errors = [{"index": {"_id": "k1", "status": 500, "error": {"type": "internal"}}}]
        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(0, errors)):
            await worker.process_batch(batch)

        queue.nack.assert_called_once()

    async def test_es_400_acks_and_logs(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        worker = _make_worker(mongo=mongo, queue=queue)
        batch = [_received("k1")]

        errors = [{"index": {"_id": "k1", "status": 400, "error": {"type": "mapping"}}}]
        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(0, errors)):
            await worker.process_batch(batch)

        queue.ack.assert_called_once()
        queue.nack.assert_not_called()


@pytest.mark.unit
class TestWorkerBackoff:
    async def test_batch_failure_triggers_backoff(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock(side_effect=ConnectionError("down"))
        queue = AsyncMock()
        shutdown_event = asyncio.Event()

        worker = _make_worker(mongo=mongo, queue=queue, shutdown_event=shutdown_event)

        call_count = 0
        async def drain_side_effect(max_items, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [_received()]
            shutdown_event.set()
            return []

        queue.drain = AsyncMock(side_effect=drain_side_effect)

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await worker.run()

        mock_sleep.assert_called_once()
        delay = mock_sleep.call_args[0][0]
        assert delay > 0

    async def test_consecutive_failures_increase_backoff(self):
        worker = _make_worker()
        worker._consecutive_failures = 4
        expected_base = min(2.0 * (2 ** 4), 60.0)
        assert expected_base == 32.0

    async def test_success_resets_failure_counter(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        shutdown_event = asyncio.Event()

        worker = _make_worker(mongo=mongo, queue=queue, shutdown_event=shutdown_event)
        worker._consecutive_failures = 5

        call_count = 0
        async def drain_side_effect(max_items, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [_received()]
            shutdown_event.set()
            return []

        queue.drain = AsyncMock(side_effect=drain_side_effect)

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])):
            await worker.run()

        assert worker._consecutive_failures == 0


@pytest.mark.unit
class TestWorkerShutdown:
    async def test_shutdown_event_stops_worker(self):
        queue = AsyncMock()
        shutdown_event = asyncio.Event()
        shutdown_event.set()
        queue.drain = AsyncMock(return_value=[])
        worker = _make_worker(queue=queue, shutdown_event=shutdown_event)
        await worker.run()  # should exit cleanly

    async def test_processes_batch_before_shutdown(self):
        mongo = AsyncMock()
        mongo.insert_many = AsyncMock()
        queue = AsyncMock()
        shutdown_event = asyncio.Event()

        worker = _make_worker(mongo=mongo, queue=queue, shutdown_event=shutdown_event)

        call_count = 0
        async def drain_side_effect(max_items, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [_received()]
            shutdown_event.set()
            return []

        queue.drain = AsyncMock(side_effect=drain_side_effect)

        with patch("src.ingestion.worker.async_bulk", new_callable=AsyncMock, return_value=(1, [])):
            await worker.run()

        mongo.insert_many.assert_called_once()

    async def test_is_alive_heartbeat(self):
        worker = _make_worker()
        assert not worker.is_alive()
        worker._last_heartbeat = datetime.now(timezone.utc)
        assert worker.is_alive()
