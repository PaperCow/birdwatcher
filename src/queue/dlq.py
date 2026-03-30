from __future__ import annotations
import asyncio
from src.queue.base import QueueMessage
from src.core.logging import get_logger

logger = get_logger(component="dlq")


class DeadLetterQueue:
    """In-memory dead letter queue for messages that exhaust retries.

    Limitations: messages are not persisted and are lost on restart, and there
    is no mechanism to inspect or redrive them. These are acceptable trade-offs
    for the current in-memory queue implementation — when the queue system moves
    to SQS, DLQ visibility and redrive will be handled natively.
    """

    def __init__(self, maxsize: int):
        self._queue: asyncio.Queue[QueueMessage] = asyncio.Queue(maxsize=maxsize)

    async def put(self, message: QueueMessage) -> None:
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            logger.error(
                "dlq_overflow_dropped",
                message_id=message.message_id,
                last_error=message.last_error,
                retry_count=message.retry_count,
            )

    def qsize(self) -> int:
        return self._queue.qsize()

    async def get(self) -> QueueMessage:
        return await self._queue.get()
