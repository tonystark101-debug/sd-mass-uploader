"""UTC timestamps, used for everything stored."""

from datetime import UTC, datetime


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value)


def age_seconds(value: str) -> float:
    return (datetime.now(UTC) - parse_utc(value)).total_seconds()
