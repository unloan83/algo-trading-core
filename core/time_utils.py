from datetime import datetime
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")


def now_ist_naive(value: datetime | None = None) -> datetime:
    """Return a naive datetime whose wall-clock value is consistently IST."""
    timestamp = value or datetime.now(IST)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(IST).replace(tzinfo=None)
    return timestamp
