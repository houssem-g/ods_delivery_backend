"""Base44 date format: naive ISO-8601 UTC with microseconds (the front appends 'Z')."""

from datetime import UTC, datetime


def legacy_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds")


def parse_legacy_datetime(value: str) -> datetime:
    """Accepts '...Z', '+hh:mm' offsets and naive strings (taken as UTC, like Base44)."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
