"""Timezone utilities."""
from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)
