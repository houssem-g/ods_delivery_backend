"""Server date format: ISO-8601 UTC with microseconds and an explicit « Z ».

Naive UTC strings, read as local time by a phone set to Tunis (UTC+1), a fresh
notification looked one hour old and was dropped (QA 06/10, B18). Every date the API returns
now carries its zone, so any parser reads the same instant.
"""

from datetime import UTC, datetime


def legacy_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds") + "Z"


def parse_legacy_datetime(value: str) -> datetime:
    """Accepts '...Z', '+hh:mm' offsets and naive strings (taken as UTC)."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
