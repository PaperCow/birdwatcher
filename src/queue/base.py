from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
import uuid


@dataclass
class QueueMessage:
    payload: dict[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    retry_count: int = 0
    enqueued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ReceivedMessage:
    queue_message: QueueMessage
    receipt_handle: str


class EventQueue(Protocol):
    async def enqueue(self, message: QueueMessage) -> None: ...
    async def drain(self, max_items: int, timeout: float) -> list[ReceivedMessage]: ...
    async def ack(self, receipt_handle: str) -> None: ...
    async def nack(self, receipt_handle: str) -> None: ...
    async def qsize(self) -> int: ...
