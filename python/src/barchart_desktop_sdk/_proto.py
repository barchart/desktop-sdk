"""Reading protocol values: times and trade dates."""

from datetime import UTC, date, datetime, timedelta

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def to_time(nanos: int) -> datetime | None:
    """Nanoseconds since the Unix epoch, as a UTC datetime truncated to microseconds; 0 is proto3's "not set"."""
    if not nanos:
        return None
    return _EPOCH + timedelta(microseconds=nanos // 1000)


def to_trade_date(yyyymmdd: int) -> date | None:
    """An exchange session date (YYYYMMDD) as a date; 0 is proto3's "not set", and so is a date that doesn't exist."""
    if not yyyymmdd:
        return None
    try:
        return date(yyyymmdd // 10000, yyyymmdd // 100 % 100, yyyymmdd % 100)
    except ValueError:
        return None
