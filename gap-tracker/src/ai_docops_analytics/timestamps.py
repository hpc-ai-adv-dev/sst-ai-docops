# Copyright Hewlett Packard Enterprise Development LP.
"""Timestamp parsing shared by collection, metrics, and clustering."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_timestamp(value: Any) -> datetime | None:
    """Parse ISO, epoch-second, or epoch-millisecond values as UTC."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            numeric = float(value)
            if abs(numeric) >= 1_000_000_000_000:
                numeric /= 1000
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def normalize_timestamp(value: Any) -> str | None:
    """Return a comparable UTC ISO timestamp, or None for invalid input."""
    parsed = parse_timestamp(value)
    return parsed.isoformat() if parsed is not None else None
