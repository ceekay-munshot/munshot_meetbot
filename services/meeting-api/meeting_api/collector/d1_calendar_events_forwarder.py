"""Mirror a client's Google Calendar events into a Cloudflare D1 table, and
read them back for GET /calendar/meetings.

Postgres `CalendarEvent` (upserted/cancelled by calendar-service's sync loop
from the live Google Calendar API) is the source of truth. calendar-service
calls mirror_calendar_event_to_d1() with that same row's fields right after
each Postgres write so this D1 copy stays reconciled. Postgres never
hard-deletes CalendarEvent rows (disconnecting a calendar only clears the
stored OAuth token — see calendar-service/app/main.py's disconnect_calendar),
so this mirror only ever upserts, never deletes, matching that invariant. If
a future Postgres cleanup job starts hard-deleting events, this mirror needs
a delete path added to match, or D1 rows will orphan.

Key rules (same contract as the other D1 sinks):
  * Postgres remains the source of truth; D1 is a best-effort mirror.
  * Never raises into the caller — a D1 outage must not break calendar sync.
  * Idempotent upsert keyed on `id` (the Postgres CalendarEvent primary key).

Enable via CLOUDFLARE_D1_ENABLED=true plus the CF_* credentials (config.py).
Schema: deploy/cloudflare-d1/schema_calendar_events.sql.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .config import CF_D1_CALENDAR_EVENTS_TABLE
from .d1_client import d1_query

logger = logging.getLogger(__name__)

# Dirty-check cache: {event_id: fingerprint of last successfully mirrored row}.
#
# The sync loop re-reads the whole calendar window every tick (Google never
# issues a nextSyncToken for our request shape — see google_calendar.list_events),
# so it calls this mirror once per event per tick even when nothing changed.
# At ~40 users x ~100 events x 288 ticks/day that is ~1.1M identical D1 writes a
# day. Comparing against the last mirrored fingerprint skips the HTTP round-trip
# entirely, which is the cost that matters — a conditional UPDATE would still
# pay for the request.
#
# Safe because this process is the only writer of these rows: if the fingerprint
# matches, D1 already holds exactly these values. The cache is deliberately
# in-process and lossy — a restart just re-mirrors everything once, which
# reconciles rather than corrupts.
_MIRROR_CACHE: dict[int, tuple] = {}
# Bound the dict so a long-lived process churning through event ids can't grow
# it without limit. Clearing wholesale (rather than LRU-evicting) costs one
# extra re-mirror pass and keeps this dependency-free.
_MIRROR_CACHE_MAX = 20000


def _p(value):
    """D1's /query params are documented as strings; SQLite column affinity
    coerces numeric strings back to INTEGER/REAL on insert. Keep NULLs as
    JSON null (same convention as d1_forwarder.py's `_p`)."""
    return None if value is None else str(value)


def _to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _to_iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


async def mirror_calendar_event_to_d1(
    event_id: int,
    owner_email: Optional[str],
    title: Optional[str],
    start_time: Optional[datetime],
    end_time: Optional[datetime],
    meeting_url: Optional[str],
    platform: Optional[str],
    status: Optional[str],
    meeting_id: Optional[int],
) -> None:
    """Best-effort upsert of one calendar event row into D1. Never raises.

    No-ops when this exact row was already mirrored successfully (see
    _MIRROR_CACHE) — the sync loop re-sends every event every tick.
    """
    if event_id is None:
        return

    normalized_email = (owner_email or "").strip().lower() or None
    fingerprint = (
        normalized_email,
        title,
        _to_ms(start_time),
        _to_ms(end_time),
        meeting_url,
        platform,
        status,
        meeting_id,
    )
    if _MIRROR_CACHE.get(event_id) == fingerprint:
        return

    sql = (
        f"INSERT INTO {CF_D1_CALENDAR_EVENTS_TABLE} "
        "(id, owner_email, title, start_time_ms, end_time_ms, meeting_url, platform, status, meeting_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (id) DO UPDATE SET "
        "owner_email=excluded.owner_email, title=excluded.title, "
        "start_time_ms=excluded.start_time_ms, end_time_ms=excluded.end_time_ms, "
        "meeting_url=excluded.meeting_url, platform=excluded.platform, "
        "status=excluded.status, meeting_id=excluded.meeting_id"
    )
    params = [_p(event_id)] + [_p(v) for v in fingerprint]
    result = await d1_query(sql, params)
    if result is None:
        # Drop any cached fingerprint so the next tick retries instead of
        # assuming D1 holds a row this write never landed.
        _MIRROR_CACHE.pop(event_id, None)
        logger.error("D1 calendar_events mirror failed for event %s (non-fatal)", event_id)
        return

    if len(_MIRROR_CACHE) >= _MIRROR_CACHE_MAX:
        _MIRROR_CACHE.clear()
    _MIRROR_CACHE[event_id] = fingerprint


async def query_calendar_events_from_d1(
    owner_email: str, include_cancelled: bool, now_ms: int
) -> Optional[list[dict[str, Any]]]:
    """Read a client's calendar events back out of D1 for GET /calendar/meetings.

    Returns rows already shaped for the response JSON (start_time/end_time as
    ISO8601 UTC strings, matching the previous Postgres-backed shape exactly),
    or None if D1 is unreachable/unconfigured — the caller should surface an
    error rather than silently treating that the same as "no events".
    """
    owner_email = (owner_email or "").strip().lower()
    conditions = [
        "owner_email = ?",
        "(end_time_ms >= ? OR (end_time_ms IS NULL AND start_time_ms >= ?))",
    ]
    params = [owner_email, _p(now_ms), _p(now_ms)]
    if not include_cancelled:
        conditions.append("status != 'cancelled'")

    sql = (
        "SELECT id, title, start_time_ms, end_time_ms, meeting_url, platform, status, meeting_id "
        f"FROM {CF_D1_CALENDAR_EVENTS_TABLE} WHERE {' AND '.join(conditions)} "
        "ORDER BY start_time_ms"
    )
    rows = await d1_query(sql, params)
    if rows is None:
        return None

    def _int_or_none(v):
        return int(v) if v is not None else None

    return [
        {
            "id": _int_or_none(r.get("id")),
            "title": r.get("title"),
            "start_time": _to_iso(_int_or_none(r.get("start_time_ms"))),
            "end_time": _to_iso(_int_or_none(r.get("end_time_ms"))),
            "meeting_url": r.get("meeting_url"),
            "platform": r.get("platform"),
            "status": r.get("status"),
            "meeting_id": _int_or_none(r.get("meeting_id")),
        }
        for r in rows
    ]
