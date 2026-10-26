from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def local_day(now: datetime, timezone: ZoneInfo) -> date:
    return now.astimezone(timezone).date()


def day_key(now: datetime, timezone: ZoneInfo) -> str:
    return local_day(now, timezone).isoformat()


def day_start_timestamp(now: datetime, timezone: ZoneInfo) -> float:
    start = datetime.combine(local_day(now, timezone), time.min, tzinfo=timezone)
    return start.timestamp()


def month_start_timestamp(now: datetime, timezone: ZoneInfo) -> float:
    local = now.astimezone(timezone)
    start = datetime(local.year, local.month, 1, tzinfo=timezone)
    return start.timestamp()


def scheduled_at(day: date, hour: int, minute: int, timezone: ZoneInfo) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=timezone)


def day_bounds(day: date, timezone: ZoneInfo) -> tuple[float, float]:
    start = datetime.combine(day, time.min, tzinfo=timezone)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()

