"""DST-aware "next run" computation for recurring D1 `schedules` rows.

No timezone-conversion utility exists anywhere else in this repo (all
datetime handling elsewhere is UTC-only) — this is net-new, stdlib
`zoneinfo` only (no pytz dependency exists in this repo).

`next_recurring_run` is only ever called for recurring rows (recurrence in
{'daily','weekdays','weekly'}). A `'once'` row's lifecycle — delete on
success, increment attempts and retry (same next_run) on failure — is handled
entirely by scheduler.py's dispatch loop; it is never rescheduled here.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger("calendar-service.recurrence")

# One tick's worth of table columns (weekday: Worker convention 0=Sun..6=Sat).
_VALID_RECURRENCES = {"daily", "weekdays", "weekly"}
_MAX_LOOKAHEAD_DAYS = 8


def next_recurring_run(
    now_ms: int,
    recurrence: str,
    time_zone: str,
    hour: int,
    minute: int,
    weekday: int | None = None,
) -> int:
    """Return the next occurrence (epoch ms, UTC) strictly after now_ms."""
    if recurrence not in _VALID_RECURRENCES:
        raise ValueError(
            f"next_recurring_run must not be called for recurrence={recurrence!r} "
            "('once' rows are deleted/retried by the dispatch loop, not rescheduled here)"
        )
    if recurrence == "weekly" and weekday is None:
        raise ValueError("weekly recurrence requires a weekday (0=Sun..6=Sat)")

    tz = ZoneInfo(time_zone)
    now_utc = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    # Anchor "today" in the TARGET tz, not the server's/UTC's — otherwise a
    # tick running near midnight can walk forward from the wrong calendar day.
    anchor_date = now_utc.astimezone(tz).date()

    for offset in range(_MAX_LOOKAHEAD_DAYS):
        candidate_date = anchor_date + timedelta(days=offset)
        if not _matches_recurrence(candidate_date, recurrence, weekday):
            continue
        candidate_utc = _wall_time_to_utc(candidate_date, hour, minute, tz)
        if candidate_utc > now_utc:
            return int(candidate_utc.timestamp() * 1000)

    raise ValueError(
        f"no matching day found within {_MAX_LOOKAHEAD_DAYS} days "
        f"for recurrence={recurrence!r} weekday={weekday!r}"
    )


def _matches_recurrence(candidate_date: date, recurrence: str, weekday: int | None) -> bool:
    if recurrence == "daily":
        return True
    if recurrence == "weekdays":
        return candidate_date.isoweekday() <= 5  # Mon(1)..Fri(5)
    # 'weekly': column convention is 0=Sun..6=Sat. date.isoweekday() is
    # Mon=1..Sun=7, so `% 7` remaps Sun 7->0, Mon 1->1, ..., Sat 6->6 — matches.
    return (candidate_date.isoweekday() % 7) == weekday


def _wall_time_to_utc(candidate_date: date, hour: int, minute: int, tz: ZoneInfo) -> datetime:
    """Convert a local wall-clock time to UTC, resolving DST edge cases
    explicitly rather than relying on zoneinfo's silent defaults.

    - Fall-back (a wall time occurs twice, e.g. 1:30 AM on the "clocks back"
      night): resolved with `fold=0` — the FIRST/pre-transition occurrence.
      Deliberate choice, stated here rather than left to the default.
    - Spring-forward (a wall time doesn't exist, e.g. 2:30 AM on the "clocks
      forward" day): `fold=0` resolves a gap time using the offset in effect
      *before* the transition, which converting back through the (now
      correct, post-transition) offset lands on `requested_time + gap_size`
      — i.e. the wall-clock-equivalent instant just after the jump (2:30 AM
      in a 1-hour gap resolves to 3:30 AM). This is a standard, defensible
      resolution; detect it and log rather than let it pass silently.
    """
    naive = datetime(candidate_date.year, candidate_date.month, candidate_date.day, hour, minute)
    local_dt = naive.replace(tzinfo=tz, fold=0)
    utc_dt = local_dt.astimezone(timezone.utc)

    roundtrip = utc_dt.astimezone(tz)
    if (roundtrip.hour, roundtrip.minute) != (hour, minute):
        logger.warning(
            "Requested wall time %02d:%02d on %s in %s falls in a DST "
            "spring-forward gap; resolved to %02d:%02d instead",
            hour, minute, candidate_date, tz.key, roundtrip.hour, roundtrip.minute,
        )
    return utc_dt
