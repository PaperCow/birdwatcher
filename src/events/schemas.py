from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import orjson
from pydantic import AwareDatetime, BaseModel, Field, field_validator

from src.config import get_settings


def _check_depth(obj: Any, current: int = 1, max_depth: int = 10) -> None:
    """Recursively check that a nested dict/list does not exceed *max_depth*."""
    if current > max_depth:
        raise ValueError(f"metadata exceeds maximum depth of {max_depth}")
    if isinstance(obj, dict):
        for v in obj.values():
            _check_depth(v, current + 1, max_depth)
    elif isinstance(obj, list):
        for v in obj:
            _check_depth(v, current + 1, max_depth)


class EventCreate(BaseModel):
    idempotency_key: str
    event_type: str
    timestamp: AwareDatetime
    user_id: str
    source_url: str
    metadata: Optional[dict[str, Any]] = None

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, v: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if v is None:
            return v
        settings = get_settings()
        # Size check
        serialized = orjson.dumps(v)
        if len(serialized) > settings.max_metadata_size:
            raise ValueError(
                f"metadata exceeds maximum size of {settings.max_metadata_size} bytes"
            )
        # Depth check
        _check_depth(v, current=1, max_depth=settings.max_metadata_depth)
        return v

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, v: datetime) -> datetime:
        settings = get_settings()
        now = datetime.now(timezone.utc)
        earliest = now - timedelta(days=settings.timestamp_past_window_days)
        latest = now + timedelta(minutes=settings.timestamp_future_window_minutes)
        if v < earliest:
            raise ValueError(
                f"timestamp is too far in the past (max {settings.timestamp_past_window_days} days)"
            )
        if v > latest:
            raise ValueError(
                f"timestamp is too far in the future (max {settings.timestamp_future_window_minutes} minutes)"
            )
        return v


class EventAccepted(BaseModel):
    idempotency_key: str
    event_type: str
    timestamp: datetime
    user_id: str
    source_url: str
    metadata: Optional[dict[str, Any]] = None


class EventResponse(BaseModel):
    id: str
    idempotency_key: str
    event_type: str
    timestamp: datetime
    user_id: str
    source_url: str
    metadata: Optional[dict[str, Any]] = None
    created_at: datetime

    @classmethod
    def from_mongo(cls, doc: dict[str, Any]) -> EventResponse:
        return cls(
            id=str(doc["_id"]),
            idempotency_key=doc["idempotency_key"],
            event_type=doc["event_type"],
            timestamp=doc["timestamp"],
            user_id=doc["user_id"],
            source_url=doc["source_url"],
            metadata=doc.get("metadata"),
            created_at=doc["created_at"],
        )


class EventFilter(BaseModel):
    event_type: Optional[str] = None
    user_id: Optional[str] = None
    source_url: Optional[str] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    skip: int = Field(0, ge=0)
    limit: int = Field(50, ge=1, le=200)
