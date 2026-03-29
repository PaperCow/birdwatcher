from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class SearchQuery(BaseModel):
    q: str
    event_type: Optional[str] = None
    user_id: Optional[str] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    skip: int = Field(0, ge=0)
    limit: int = Field(20, ge=1, le=100)


class SearchHit(BaseModel):
    id: str
    event_type: str
    timestamp: datetime
    user_id: str
    source_url: str
    metadata: Optional[dict[str, Any]] = None
    score: float


class SearchResponse(BaseModel):
    hits: list[SearchHit]
    total: int
