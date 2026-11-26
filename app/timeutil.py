"""Time helpers. Everything is stored and returned as UTC."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator

UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    """Normalize an aware datetime to UTC. Naive datetimes are rejected by the
    API layer before reaching here; if one slips through it is assumed UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def iso(value: datetime | None) -> str | None:
    """ISO 8601 in UTC, always with microseconds and a trailing Z,
    e.g. 2026-11-26T14:30:06.839792Z."""
    if value is None:
        return None
    return to_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class UTCDateTime(TypeDecorator):
    """Stores naive UTC in SQLite, always hands back aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return to_utc(value).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC)
