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
    last_error: str | None = None
    error_history: list[str] = field(default_factory=list)


class EventQueue(Protocol):
    async def enqueue(self, message: QueueMessage) -> None: ...
    async def drain(self, max_items: int, timeout: float) -> list: ...
    async def ack(self, message_id: str) -> None: ...
    async def nack(self, message: QueueMessage, error: str) -> None: ...
    def qsize(self) -> int: ...
