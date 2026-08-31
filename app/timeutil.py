"""Timezone resolution and time formatting -- one resolver for the whole app.

v1 split this: the scheduler resolved TZ through zoneinfo, while every displayed
timestamp went through libc localtime. Those disagree whenever the container's
system zone and the TZ variable differ, which is the normal case on a NAS. Both
paths go through `local_tz()` here.

`resolve()` returns None rather than raising on an unknown zone. Passing a bad
value into APScheduler raises at startup and takes the whole app down, which is
a silly way to die over a typo'd TZ.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config

log = logging.getLogger(__name__)

_warned: set[str] = set()


def resolve(name: str | None) -> ZoneInfo | None:
    """A real zone, or None when the name is empty or unknown."""
    cleaned = (name or "").strip()
    if not cleaned:
        return None
    try:
        return ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        if cleaned not in _warned:
            _warned.add(cleaned)
            log.warning(
                "TZ=%r is not a timezone this container knows about; falling "
                "back to the system default. Use a tz database name such as "
                "Europe/London or America/New_York.",
                cleaned,
            )
        return None


def local_tz() -> ZoneInfo | None:
    return resolve(config.TZ)


def tz_name() -> str:
    zone = local_tz()
    return str(zone) if zone else (time.tzname[0] if time.tzname else "system default")


def now() -> int:
    """Epoch seconds. The engine takes `now` explicitly; this is its only source."""
    return int(time.time())


def to_local(ts: int | float | None) -> datetime | None:
    if not ts:
        return None
    try:
        moment = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    zone = local_tz()
    return moment.astimezone(zone) if zone else moment.astimezone()


def format_ts(ts: int | float | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    moment = to_local(ts)
    return moment.strftime(fmt) if moment else "-"


def iso(ts: int | float | None) -> str | None:
    moment = to_local(ts)
    return moment.isoformat(timespec="seconds") if moment else None


def format_duration(seconds: int | float | None) -> str:
    """Human-readable elapsed time for run summaries."""
    if seconds is None:
        return "-"
    total = int(max(0, seconds))
    if total < 60:
        return f"{total}s"
    delta = timedelta(seconds=total)
    hours, rest = divmod(int(delta.total_seconds()), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s"


def relative(ts: int | float | None, reference: int | None = None) -> str:
    """'3 days ago' style, for activity feeds."""
    if not ts:
        return "never"
    delta = (reference if reference is not None else now()) - int(ts)
    if delta < 0:
        delta = 0
    if delta < 60:
        return "just now"
    for size, label in ((86400 * 365, "year"), (86400 * 30, "month"),
                        (86400 * 7, "week"), (86400, "day"),
                        (3600, "hour"), (60, "minute")):
        if delta >= size:
            count = delta // size
            return f"{count} {label}{'' if count == 1 else 's'} ago"
    return "just now"
