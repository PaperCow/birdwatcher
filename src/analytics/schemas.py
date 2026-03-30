from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, model_validator

from src.config import get_settings


class TimeBucket(str, Enum):
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"


class StatsWindow(str, Enum):
    LAST_1H = "1h"
    LAST_6H = "6h"
    LAST_24H = "24h"


class StatsQuery(BaseModel):
    time_bucket: TimeBucket
    event_type: Optional[str] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None

    @model_validator(mode="after")
    def validate_date_range(self) -> StatsQuery:
        if self.start_date is None or self.end_date is None:
            return self
        settings = get_settings()
        # Cap output size: e.g. hourly over 7 days = ~168 buckets vs hourly over 365 days = ~8760
        max_days_map = {
            TimeBucket.HOURLY: settings.hourly_max_days,
            TimeBucket.DAILY: settings.daily_max_days,
            TimeBucket.WEEKLY: settings.weekly_max_days,
        }
        max_days = max_days_map[self.time_bucket]
        range_days = (self.end_date - self.start_date).days
        if range_days > max_days:
            raise ValueError(
                f"Date range of {range_days} days exceeds maximum of "
                f"{max_days} days for {self.time_bucket.value} buckets"
            )
        return self


class RealtimeStatsQuery(BaseModel):
    window: StatsWindow = StatsWindow.LAST_1H
    event_type: Optional[str] = None


class StatsBucket(BaseModel):
    time_bucket: datetime
    event_type: str
    count: int
