# src/tests/unit/test_worker.py
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from pymongo.errors import BulkWriteError
from src.ingestion.worker import EventWorker
from src.queue.base import QueueMessage


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
