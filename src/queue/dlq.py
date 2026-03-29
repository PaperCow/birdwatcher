from __future__ import annotations
import asyncio
from src.queue.base import QueueMessage
from src.core.logging import get_logger

logger = get_logger(component="dlq")


class DeadLetterQueue:
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
