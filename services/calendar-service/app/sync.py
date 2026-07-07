"""Calendar sync loop — polls Google Calendar, upserts events, schedules bots."""

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from meeting_api.models import CalendarEvent
from meeting_api.owners import upsert_meeting_owners
from meeting_api.collector.d1_owners_forwarder import mirror_meeting_owners_to_d1
from admin_models.models import User
from app.google_calendar import (
    refresh_access_token,
    list_events,
    extract_meeting_url,
    detect_platform,
    parse_event_time,
    CalendarTokenRevoked,
)

logger = logging.getLogger("calendar-service.sync")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
MEETING_API_URL = os.getenv("MEETING_API_URL", "http://meeting-api:8080")
BOT_API_TOKEN = os.getenv("BOT_API_TOKEN", "")
DEFAULT_LEAD_TIME_MINUTES = int(os.getenv("DEFAULT_LEAD_TIME_MINUTES", "2"))
# How far ahead to pull events from Google Calendar on each sync.
CALENDAR_SYNC_WINDOW_DAYS = int(os.getenv("CALENDAR_SYNC_WINDOW_DAYS", "30"))
# Display name for auto-dispatched bots (matches the product default).
CALENDAR_BOT_NAME = os.getenv("DEFAULT_BOT_NAME", "munshot meetbot")
# This fork is Google-Meet-only; ignore Zoom/Teams links found on calendars.
GOOGLE_MEET_ONLY = os.getenv("CALENDAR_GOOGLE_MEET_ONLY", "true").lower() in ("1", "true", "yes")


async def _list_all_events(
    access_token: str,
    time_min: Optional[datetime] = None,
    time_max: Optional[datetime] = None,
    sync_token: Optional[str] = None,
) -> tuple[list, Optional[str], bool]:
    """Follow Google's nextPageToken until exhausted so a wide time_min/time_max
    window (e.g. 30 days) doesn't silently truncate to the first page of results.

    Returns (all_items, next_sync_token, full_sync_required). nextSyncToken only
    ever appears on the final page, so it's safe to just keep the last one seen.
    """
    all_items: list = []
    next_sync_token: Optional[str] = None
    page_token: Optional[str] = None

    while True:
        resp = await list_events(
            access_token,
            time_min=time_min,
            time_max=time_max,
            sync_token=sync_token,
            page_token=page_token,
        )
        if resp.get("fullSyncRequired"):
            return [], None, True

        all_items.extend(resp.get("items", []))
        next_sync_token = resp.get("nextSyncToken") or next_sync_token
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return all_items, next_sync_token, False


async def sync_user_calendar(user_id: int, db: AsyncSession) -> int:
    """Sync a single user's Google Calendar events. Returns count of upserted events."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        logger.warning(f"User {user_id} not found")
        return 0

    user_data = user.data or {}
    gc_data = user_data.get("google_calendar", {})
    oauth = gc_data.get("oauth", {})
    refresh_token = oauth.get("refresh_token")
    if not refresh_token:
        logger.info(f"User {user_id} has no Google Calendar refresh token")
        return 0

    # Refresh access token. If Google says the token is permanently dead
    # (invalid_grant), auto-disconnect the account: drop the stored oauth block
    # so the sync loop stops hammering a dead token and the user falls back into
    # the normal "not connected" path (frontend shows the reconnect prompt).
    # Only invalid_grant triggers this — transient 5xx/network errors propagate
    # and get retried on the next sync, never deleting a healthy token.
    try:
        access_token, expires_in = await refresh_access_token(
            GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, refresh_token
        )
    except CalendarTokenRevoked:
        gc_data.pop("oauth", None)
        gc_data.pop("sync_token", None)  # stale once the grant is gone
        user_data["google_calendar"] = gc_data
        await db.execute(
            update(User).where(User.id == user_id).values(data=user_data)
        )
        await db.commit()
        logger.warning(
            f"User {user_id}: refresh token expired/revoked — auto-disconnected "
            f"calendar; user must reconnect via /calendar/connect/start"
        )
        raise

    # Get existing sync token for incremental sync
    existing_sync_token = gc_data.get("sync_token")

    time_min = datetime.now(timezone.utc)
    time_max = time_min + timedelta(days=CALENDAR_SYNC_WINDOW_DAYS)

    events, next_sync_token, full_sync_required = await _list_all_events(
        access_token, time_min=time_min, time_max=time_max, sync_token=existing_sync_token,
    )

    if full_sync_required:
        logger.info(f"Full sync required for user {user_id}, clearing sync token")
        events, next_sync_token, _ = await _list_all_events(
            access_token, time_min=time_min, time_max=time_max,
        )
    upserted = 0

    for event in events:
        event_id = event.get("id")
        if not event_id:
            continue

        # Skip cancelled events
        if event.get("status") == "cancelled":
            await db.execute(
                update(CalendarEvent)
                .where(
                    CalendarEvent.user_id == user_id,
                    CalendarEvent.external_event_id == event_id,
                )
                .values(status="cancelled")
            )
            continue

        start_time = parse_event_time(event, "start")
        if not start_time:
            continue  # All-day event, skip

        end_time = parse_event_time(event, "end")
        meeting_url = extract_meeting_url(event)
        platform = detect_platform(meeting_url) if meeting_url else None
        # Keep the raw attendee list — every attendee becomes a meeting owner
        # (co-viewer) when the bot is scheduled. Google omits this field for
        # solo events; store [] then rather than NULL so re-syncs are stable.
        attendees = event.get("attendees") or []

        stmt = pg_insert(CalendarEvent).values(
            user_id=user_id,
            external_event_id=event_id,
            title=event.get("summary", ""),
            start_time=start_time,
            end_time=end_time,
            meeting_url=meeting_url,
            platform=platform,
            attendees=attendees,
            status="pending",
        ).on_conflict_do_update(
            constraint="uq_calendar_event_user_ext_id",
            set_={
                "title": event.get("summary", ""),
                "start_time": start_time,
                "end_time": end_time,
                "meeting_url": meeting_url,
                "platform": platform,
                "attendees": attendees,
            },
        )
        await db.execute(stmt)
        upserted += 1

    # Save new sync token
    if next_sync_token:
        gc_data["sync_token"] = next_sync_token
        user_data["google_calendar"] = gc_data
        await db.execute(
            update(User).where(User.id == user_id).values(data=user_data)
        )

    await db.commit()
    logger.info(f"Synced {upserted} events for user {user_id}")
    return upserted


async def schedule_upcoming_bots(db: AsyncSession) -> int:
    """Check for pending events within lead time and schedule bots. Returns count scheduled."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(minutes=DEFAULT_LEAD_TIME_MINUTES)

    result = await db.execute(
        select(CalendarEvent).where(
            CalendarEvent.status == "pending",
            CalendarEvent.start_time <= cutoff,
            CalendarEvent.start_time >= now - timedelta(minutes=5),
            CalendarEvent.meeting_url.isnot(None),
            CalendarEvent.platform.isnot(None),
        )
    )
    events = result.scalars().all()
    scheduled = 0

    for event in events:
        # Google-Meet-only fork: skip Zoom/Teams links found on calendars.
        if GOOGLE_MEET_ONLY and event.platform != "google_meet":
            await db.execute(
                update(CalendarEvent).where(CalendarEvent.id == event.id).values(status="skipped")
            )
            continue

        user_result = await db.execute(select(User).where(User.id == event.user_id))
        user = user_result.scalar_one_or_none()
        if not user:
            continue

        # Respect the user's auto-join preference (default on when unset).
        prefs = ((user.data or {}).get("google_calendar", {}) or {}).get("preferences", {})
        if prefs.get("auto_join") is False:
            continue

        try:
            # Own the meeting by the EVENT's user — inject the trusted identity
            # headers meeting-api reads (same contract the api-gateway uses). Without
            # this, every auto-joined transcript would land under BOT_API_TOKEN's
            # single account instead of the calendar owner. See meeting_api/auth.py.
            headers = {
                "X-User-ID": str(event.user_id),
                "X-User-Scopes": "bot",
                "X-User-Limits": str(getattr(user, "max_concurrent_bots", 1) or 1),
            }
            if BOT_API_TOKEN:
                headers["X-API-Key"] = BOT_API_TOKEN
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{MEETING_API_URL}/bots",
                    json={
                        "platform": event.platform,
                        "native_meeting_id": _extract_native_id(event.meeting_url, event.platform),
                        "bot_name": CALENDAR_BOT_NAME,
                        # Carry the calendar event title into the meeting so lists
                        # show e.g. "Frontend Meeting Munshot" instead of "Meeting <id>".
                        "name": event.title or None,
                    },
                    headers=headers,
                    timeout=30,
                )

            if resp.status_code in (200, 201):
                resp_data = resp.json()
                meeting_id = resp_data.get("id")
                await db.execute(
                    update(CalendarEvent)
                    .where(CalendarEvent.id == event.id)
                    .values(
                        status="scheduled",
                        meeting_id=meeting_id,
                    )
                )
                scheduled += 1
                logger.info(f"Scheduled bot for event {event.id}: {event.title}")

                # Multi-owner: every calendar attendee (+ the organizer) becomes an
                # owner of this meeting so they can each see it. Auto-creates users
                # for unknown emails. Best-effort — never let it break scheduling.
                if meeting_id is not None:
                    try:
                        owners = await upsert_meeting_owners(
                            db,
                            meeting_id=meeting_id,
                            attendees=event.attendees,
                            primary_user_id=event.user_id,
                            primary_email=getattr(user, "email", None),
                        )
                        # Mirror owner list to D1 so the frontend can show this
                        # meeting to every owner (best-effort, never raises).
                        await mirror_meeting_owners_to_d1(
                            meeting_id, [o.email for o in owners]
                        )
                    except Exception as e:
                        logger.error(
                            f"Owner materialization failed for meeting {meeting_id} "
                            f"(event {event.id}), non-fatal: {e}"
                        )
            elif resp.status_code == 409 and _existing_meeting_id(resp) is not None:
                # Someone else's bot is already in this meeting (e.g. the
                # organizer's calendar synced first). Don't treat this as a
                # failure — just attach this event's user as an owner/viewer
                # of the meeting that's already running.
                meeting_id = _existing_meeting_id(resp)
                await db.execute(
                    update(CalendarEvent)
                    .where(CalendarEvent.id == event.id)
                    .values(status="scheduled", meeting_id=meeting_id)
                )
                scheduled += 1
                logger.info(
                    f"Event {event.id} ({event.title}): meeting {meeting_id} already has an "
                    f"active bot from another user — joining as owner instead of duplicating it"
                )
                try:
                    owners = await upsert_meeting_owners(
                        db,
                        meeting_id=meeting_id,
                        attendees=event.attendees,
                        primary_user_id=event.user_id,
                        primary_email=getattr(user, "email", None),
                    )
                    await mirror_meeting_owners_to_d1(
                        meeting_id, [o.email for o in owners]
                    )
                except Exception as e:
                    logger.error(
                        f"Owner materialization failed for existing meeting {meeting_id} "
                        f"(event {event.id}), non-fatal: {e}"
                    )
            else:
                logger.error(f"Bot request failed for event {event.id}: {resp.status_code} {resp.text}")
                await db.execute(
                    update(CalendarEvent)
                    .where(CalendarEvent.id == event.id)
                    .values(status="failed")
                )
        except Exception as e:
            logger.error(f"Failed to schedule bot for event {event.id}: {e}")

    await db.commit()
    return scheduled


def _existing_meeting_id(resp: httpx.Response) -> Optional[int]:
    """Pull `detail.existing_meeting_id` out of meeting-api's 409 body, if present."""
    try:
        detail = resp.json().get("detail")
        return detail.get("existing_meeting_id") if isinstance(detail, dict) else None
    except Exception:
        return None


def _extract_native_id(url: str, platform: str) -> str:
    """Extract the native meeting ID from a URL for meeting-api."""
    if platform == "google_meet":
        # https://meet.google.com/abc-defg-hij -> abc-defg-hij
        return url.rsplit("/", 1)[-1].split("?")[0]
    if platform == "zoom":
        # https://zoom.us/j/123456?pwd=xxx -> 123456
        import re
        match = re.search(r"/j/(\d+)", url)
        return match.group(1) if match else url
    if platform == "teams":
        return url
    return url
