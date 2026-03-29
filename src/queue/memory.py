from __future__ import annotations
import asyncio
from src.queue.base import QueueMessage

SENTINEL = object()


class InMemoryQueue:
    def __init__(self, maxsize: int, dlq, max_retries: int = 5):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._dlq = dlq
        self._max_retries = max_retries

    async def enqueue(self, message: QueueMessage) -> None:
        self._queue.put_nowait(message)  # raises asyncio.QueueFull

    async def drain(self, max_items: int, timeout: float) -> list:
        items = []
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            items.append(first)
            if first is SENTINEL:
                return items
        except asyncio.TimeoutError:
            return items

        while len(items) < max_items:
            try:
                item = self._queue.get_nowait()
                items.append(item)
                if item is SENTINEL:
                    return items
            except asyncio.QueueEmpty:
                break
        return items

    async def ack(self, message_id: str) -> None:
        self._queue.task_done()

    async def nack(self, message: QueueMessage, error: str) -> None:
        message.retry_count += 1
        message.last_error = error
        message.error_history.append(error)
        self._queue.task_done()

        if message.retry_count >= self._max_retries:
            await self._dlq.put(message)
            return
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            await self._dlq.put(message)

    def qsize(self) -> int:
        return self._queue.qsize()

    async def join(self) -> None:
        await self._queue.join()

    async def shutdown(self) -> None:
        await self._queue.put(SENTINEL)

    @property
    def sentinel(self):
        return SENTINEL
