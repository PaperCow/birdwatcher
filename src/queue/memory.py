from __future__ import annotations
import asyncio
from src.queue.base import QueueMessage, ReceivedMessage
from src.queue.dlq import DeadLetterQueue
from src.core.exceptions import QueueFullError
from src.core.logging import get_logger

logger = get_logger(component="queue")

SENTINEL = object()


class InMemoryQueue:
    def __init__(self, maxsize: int, max_retries: int = 5, dlq_maxsize: int = 1000):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._dlq = DeadLetterQueue(maxsize=dlq_maxsize)
        self._max_retries = max_retries
        self._in_flight: dict[str, QueueMessage] = {}

    async def enqueue(self, message: QueueMessage) -> None:
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            raise QueueFullError("Event queue is at capacity")

    async def drain(self, max_items: int, timeout: float) -> list[ReceivedMessage]:
        items: list[ReceivedMessage] = []
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            if first is SENTINEL:
                return items
            receipt_handle = first.message_id
            self._in_flight[receipt_handle] = first
            items.append(ReceivedMessage(queue_message=first, receipt_handle=receipt_handle))
        except asyncio.TimeoutError:
            return items

        while len(items) < max_items:
            try:
                item = self._queue.get_nowait()
                if item is SENTINEL:
                    return items
                receipt_handle = item.message_id
                self._in_flight[receipt_handle] = item
                items.append(ReceivedMessage(queue_message=item, receipt_handle=receipt_handle))
            except asyncio.QueueEmpty:
                break
        return items

    async def ack(self, receipt_handle: str) -> None:
        self._in_flight.pop(receipt_handle, None)
        self._queue.task_done()

    async def nack(self, receipt_handle: str) -> None:
        message = self._in_flight.pop(receipt_handle, None)
        if message is None:
            logger.warning("nack_unknown_receipt_handle", receipt_handle=receipt_handle)
            return
        message.retry_count += 1
        self._queue.task_done()

        if message.retry_count >= self._max_retries:
            await self._dlq.put(message)
            return
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            await self._dlq.put(message)

    async def qsize(self) -> int:
        return self._queue.qsize()

    def dlq_qsize(self) -> int:
        return self._dlq.qsize()

    async def join(self) -> None:
        await self._queue.join()

    async def shutdown(self) -> None:
        await self._queue.put(SENTINEL)
