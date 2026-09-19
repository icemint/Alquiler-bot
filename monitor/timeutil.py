"""Time helpers.

Everything is stored in UTC; everything shown to the user is rendered in the
configured local timezone (Buenos Aires by default).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

DEFAULT_TZ_NAME = "America/Argentina/Buenos_Aires"
_FALLBACK_TZ = timezone(timedelta(hours=-3), "ART")


def local_timezone():
    name = os.getenv("TZ") or DEFAULT_TZ_NAME
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - missing tzdata must not break the service
        return _FALLBACK_TZ


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_local(value: Optional[datetime], fmt: str = "%d/%m/%Y %H:%M") -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(local_timezone()).strftime(fmt)
