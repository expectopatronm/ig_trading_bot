"""
sessions.py — Session window filtering in Europe/Berlin.
Rarely changed; pulled out of main.
"""

from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

from config import (
    SESSION_FILTER_ENABLED, SESSION_SKIP_WEEKENDS, SESSION_WINDOWS_LOCAL
)


def _tz_berlin() -> ZoneInfo:
    return ZoneInfo("Europe/Berlin")


def _parse_hhmm(s: str) -> dtime:
    hh, mm = s.split(":")
    return dtime(hour=int(hh), minute=int(mm))


def is_within_sessions(now: Optional[datetime] = None) -> bool:
    """Return True if 'now' (Berlin time) sits inside any configured session window."""
    if not SESSION_FILTER_ENABLED:
        return True
    tz = _tz_berlin()
    now = now or datetime.now(tz)
    if SESSION_SKIP_WEEKENDS and now.weekday() >= 5:
        return False
    t = now.timetz()
    for start_str, end_str in SESSION_WINDOWS_LOCAL:
        start = _parse_hhmm(start_str)
        end = _parse_hhmm(end_str)
        if start <= t <= end:
            return True
    return False
