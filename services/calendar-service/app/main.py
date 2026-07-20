"""Calendar Service — Google Calendar sync + bot scheduling."""

import os
import asyncio
import hmac
import base64
import hashlib
import logging
from datetime import datetime, timezone

import httpx
import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Query, Header
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from meeting_api.database import get_db, init_db
from meeting_api.models import CalendarEvent, Meeting
from admin_models.models import User
from app.sync import (
    sync_user_calendar,
    schedule_upcoming_bots,
    _extract_native_id,
    MEETING_API_URL,
    BOT_API_TOKEN,
)
from app.scheduler import schedule_dispatch_loop
from meeting_api.collector.d1_calendar_events_forwarder import (
    mirror_calendar_event_to_d1,
    query_calendar_events_from_d1,
)
from meeting_api.collector.d1_schedule_client import delete_schedules_by_owner
from app.google_calendar import (
    build_consent_url,
    exchange_code_for_tokens,
    email_from_id_token,
    CalendarTokenRevoked,
    CalendarScopeInsufficient,
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
SYNC_INTERVAL_SECONDS = int(os.getenv("SYNC_INTERVAL_SECONDS", "300"))
# Trusted server-to-server key for the Cloudflare BFF to store a client's
# calendar OAuth token. Reuses the same system key as /public/join.
SYSTEM_KEY = os.getenv("PUBLIC_BOT_API_KEY", "") or os.getenv("CALENDAR_SYSTEM_KEY", "")

# Server-side OAuth: the gateway's public origin (e.g. https://api.example.com).
# The Google redirect URI is {PUBLIC_BASE_URL}/calendar/connect/callback and must
# be registered exactly in the Google Cloud Console OAuth client.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
# Where to bounce the user's browser after connecting, if no ?return= is given.
CALENDAR_FRONTEND_URL = os.getenv("CALENDAR_FRONTEND_URL", "").rstrip("/")
ALLOW_INSECURE_OAUTH = os.getenv("CALENDAR_ALLOW_INSECURE_OAUTH", "false").lower() in (
    "1",
    "true",
    "yes",
)

logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("calendar-service")

_VEXA_ENV = os.getenv("VEXA_ENV", "development")
_PUBLIC_DOCS = _VEXA_ENV != "production"
app = FastAPI(
    title="Calendar Service",
    description="Google Calendar sync and auto-join scheduling",
    docs_url="/docs" if _PUBLIC_DOCS else None,
    redoc_url="/redoc" if _PUBLIC_DOCS else None,
    openapi_url="/openapi.json" if _PUBLIC_DOCS else None,
)


@app.on_event("startup")
async def startup():
    await init_db()
    asyncio.create_task(sync_loop())
    asyncio.create_task(schedule_dispatch_loop())


async def sync_loop():
    """Background loop: sync all connected calendars and schedule bots."""
    while True:
        try:
            from meeting_api.database import async_session_local
            async with async_session_local() as db:
                # Find all users with google_calendar oauth configured
                result = await db.execute(select(User))
                users = result.scalars().all()
                for user in users:
                    gc = (user.data or {}).get("google_calendar", {})
                    if gc.get("oauth", {}).get("refresh_token"):
                        try:
                            await sync_user_calendar(user.id, db)
                        except CalendarTokenRevoked:
                            # Expected, self-healing: token already dropped inside
                            # sync_user_calendar. Account now shows as disconnected.
                            logger.info(
                                f"User {user.id}: calendar auto-disconnected "
                                f"(token expired/revoked) — awaiting reconnect"
                            )
                        except CalendarScopeInsufficient:
                            # Expected, self-healing: grant already dropped inside
                            # sync_user_calendar. The user consented without granting
                            # calendar access, so there is nothing to retry.
                            logger.info(
                                f"User {user.id}: calendar auto-disconnected "
                                f"(consent granted without calendar scope) — awaiting reconnect"
                            )
                        except Exception as e:
                            logger.error(f"Sync failed for user {user.id}: {e}")

                # Schedule bots for upcoming events
                await schedule_upcoming_bots(db)
        except Exception as e:
            logger.error(f"Sync loop error: {e}")

        await asyncio.sleep(SYNC_INTERVAL_SECONDS)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "calendar-service"}


class CalendarOAuthRequest(BaseModel):
    email: str
    refresh_token: str


async def _store_refresh_token(db: AsyncSession, email: str, refresh_token: str) -> tuple[int, int]:
    """Find-or-create the user by email, persist the refresh token, initial-sync.

    Returns (user_id, events_synced). The initial sync is best-effort — the
    background loop retries — so a sync failure does not lose the stored token.
    """
    email = (email or "").strip().lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user:
        user = User(email=email)
        db.add(user)
        await db.commit()
        await db.refresh(user)

    user_data = dict(user.data or {})
    gc = dict(user_data.get("google_calendar", {}))
    oauth = dict(gc.get("oauth", {}))
    oauth["refresh_token"] = refresh_token
    gc["oauth"] = oauth
    user_data["google_calendar"] = gc
    await db.execute(update(User).where(User.id == user.id).values(data=user_data))
    await db.commit()

    synced = 0
    try:
        synced = await sync_user_calendar(user.id, db)
    except Exception as e:  # non-fatal — the loop will retry
        logger.warning(f"Initial calendar sync failed for user {user.id}: {e}")
    return user.id, synced


def _sign_state(payload: str) -> str:
    """HMAC-sign a state payload so the callback can trust the return URL."""
    b = base64.urlsafe_b64encode((payload or "").encode()).decode().rstrip("=")
    sig = hmac.new((SYSTEM_KEY or "calendar-oauth").encode(), b.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{b}.{sig}"


def _unsign_state(state: str) -> str | None:
    """Verify a signed state and return the original payload, or None if tampered."""
    try:
        b, sig = (state or "").rsplit(".", 1)
        expected = hmac.new((SYSTEM_KEY or "calendar-oauth").encode(), b.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected):
            return None
        b += "=" * (-len(b) % 4)
        return base64.urlsafe_b64decode(b).decode()
    except Exception:
        return None


def _with_param(url: str, key: str, value: str) -> str:
    """Append a query param to a return URL (falls back to '/')."""
    if not url:
        url = "/"
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{key}={value}"


@app.post("/calendar/oauth")
@app.post("/public/join")
async def store_calendar_oauth(
    body: CalendarOAuthRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
):
    """Store a client's Google Calendar refresh token (server-to-server).

    Trusted callers (the Cloudflare BFF, or anyone holding the system key) can
    post a {email, refresh_token} they obtained out of band. For the fully
    automated browser flow, use GET /calendar/connect/start instead.
    """
    if (not SYSTEM_KEY) or (not x_api_key) or (not hmac.compare_digest(x_api_key, SYSTEM_KEY)):
        if not ALLOW_INSECURE_OAUTH:
            raise HTTPException(status_code=401, detail="Invalid or missing system API key")
        logger.warning(
            "CALENDAR_ALLOW_INSECURE_OAUTH enabled — skipping system API key enforcement"
        )

    email = (body.email or "").strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=422, detail="Invalid email")
    if not body.refresh_token:
        raise HTTPException(status_code=422, detail="refresh_token is required")

    user_id, synced = await _store_refresh_token(db, email, body.refresh_token)
    return {"status": "connected", "user_id": user_id, "events_synced": synced}


@app.get("/calendar/connect/start")
async def calendar_connect_start(return_url: str = Query(default="", alias="return")):
    """Begin the browser OAuth flow — 302 redirect the user to Google consent.

    The frontend links a "Connect Google Calendar" button here. No identity is
    passed: the connecting account is read from Google's id_token in the callback,
    so a client can only ever connect their own calendar.
    """
    if not (GOOGLE_CLIENT_ID and PUBLIC_BASE_URL):
        raise HTTPException(
            status_code=500,
            detail="Calendar OAuth not configured (need GOOGLE_CLIENT_ID and PUBLIC_BASE_URL)",
        )
    redirect_uri = f"{PUBLIC_BASE_URL}/calendar/connect/callback"
    state = _sign_state(return_url or CALENDAR_FRONTEND_URL)
    consent_url = build_consent_url(GOOGLE_CLIENT_ID, redirect_uri, state)
    return RedirectResponse(consent_url, status_code=302)


@app.get("/calendar/connect/callback")
async def calendar_connect_callback(
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
):
    """Google redirects here with an auth code; exchange it and store the token."""
    return_url = _unsign_state(state) or CALENDAR_FRONTEND_URL or "/"
    if error or not code:
        logger.warning(f"OAuth callback error={error!r} code_present={bool(code)}")
        return RedirectResponse(_with_param(return_url, "calendar", "error"), status_code=302)

    redirect_uri = f"{PUBLIC_BASE_URL}/calendar/connect/callback"
    try:
        tokens = await exchange_code_for_tokens(
            GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, code, redirect_uri
        )
    except Exception as e:
        logger.error(f"Token exchange failed: {e}")
        return RedirectResponse(_with_param(return_url, "calendar", "error"), status_code=302)

    refresh_token = tokens.get("refresh_token")
    email = email_from_id_token(tokens.get("id_token", ""))
    if not refresh_token or not email:
        # No refresh_token usually means consent was previously granted without
        # prompt=consent; we force prompt=consent so this should be rare.
        logger.error(f"Missing refresh_token={bool(refresh_token)} or email={email!r}")
        return RedirectResponse(_with_param(return_url, "calendar", "error"), status_code=302)

    user_id, synced = await _store_refresh_token(db, email, refresh_token)
    logger.info(f"Calendar connected via OAuth: user_id={user_id} email={email} synced={synced}")
    return RedirectResponse(_with_param(return_url, "calendar", "connected"), status_code=302)


def _check_system_key(x_api_key: str | None) -> None:
    """Enforce the trusted server-to-server key (same key as /calendar/oauth)."""
    if (not SYSTEM_KEY) or (not x_api_key) or (not hmac.compare_digest(x_api_key, SYSTEM_KEY)):
        if not ALLOW_INSECURE_OAUTH:
            raise HTTPException(status_code=401, detail="Invalid or missing system API key")
        logger.warning("CALENDAR_ALLOW_INSECURE_OAUTH enabled — skipping system API key enforcement")


def _meet_url(platform: str | None, native_id: str | None) -> str | None:
    """Reconstruct a joinable URL from a meeting's platform + native id."""
    if platform == "google_meet" and native_id:
        return f"https://meet.google.com/{native_id}"
    return None


class CalendarSyncRequest(BaseModel):
    email: str


@app.post("/calendar/sync")
async def sync_client_calendar(
    body: CalendarSyncRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
):
    """Sync a client's calendar by email (server-to-server, on demand).

    If the client has connected their Google Calendar, re-polls it immediately
    and returns how many events are now stored. If they haven't connected yet,
    returns connected=false plus a connect_url to send them through consent.
    """
    _check_system_key(x_api_key)
    email = (body.email or "").strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=422, detail="Invalid email")

    connect_url = f"{PUBLIC_BASE_URL}/calendar/connect/start" if PUBLIC_BASE_URL else None
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user or not (user.data or {}).get("google_calendar", {}).get("oauth", {}).get("refresh_token"):
        return {
            "connected": False,
            "user_id": user.id if user else None,
            "events_synced": 0,
            "connect_url": connect_url,
            "detail": "Calendar not connected — send the client to connect_url to authorize.",
        }

    try:
        synced = await sync_user_calendar(user.id, db)
    except CalendarTokenRevoked:
        # sync_user_calendar has already dropped the dead token — report the
        # account as (now) disconnected so the caller sends the client back
        # through consent, instead of surfacing a hard 502.
        return {
            "connected": False,
            "user_id": user.id,
            "events_synced": 0,
            "connect_url": connect_url,
            "detail": "Calendar token expired or revoked — reconnect required.",
        }
    except CalendarScopeInsufficient:
        # Live grant, but the user completed consent without ticking the
        # calendar checkbox — so it can read nothing. sync_user_calendar has
        # already dropped it; same reconnect path as a revoked token (a scope
        # cannot be added to an existing grant). Without this branch every sync
        # for that client 502s forever while the account still looks connected.
        return {
            "connected": False,
            "user_id": user.id,
            "events_synced": 0,
            "connect_url": connect_url,
            "detail": (
                "Calendar permission was not granted — reconnect and allow "
                "calendar access to continue."
            ),
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Calendar sync failed: {e}")
    return {"connected": True, "user_id": user.id, "events_synced": synced}


@app.get("/calendar/meetings")
async def list_client_meetings(
    email: str = Query(...),
    include_cancelled: bool = Query(default=False),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
):
    """List every meeting for a client by email — calendar-scheduled and manual.

    Returns two arrays:
      - calendar_events: upcoming events pulled from their Google Calendar
        (status pending → scheduled once a bot is dispatched). Removed events
        (status='cancelled') are excluded unless include_cancelled=true, which
        lets the frontend build a "removed meetings" view to restore from.
        Read from the Cloudflare D1 mirror (see
        meeting_api.collector.d1_calendar_events_forwarder) rather than
        Postgres directly — Postgres CalendarEvent remains the source of
        truth and is what populates D1 on every sync/remove/restore.
      - meetings: actual bot meetings (Meeting rows). source='calendar' if the
        meeting was auto-dispatched from a calendar event, else 'manual'.
    """
    _check_system_key(x_api_key)
    email = (email or "").strip().lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Client not found")

    # Only meetings that WILL happen (or are happening now) — the calendar sync
    # writes future events but never prunes ones that have since passed, so we
    # filter to not-yet-ended here. Prefer end_time; fall back to start_time
    # when an event has no end. Same filtering semantics as the old
    # Postgres-backed query, now applied inside the D1 SQL itself.
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    calendar_events = await query_calendar_events_from_d1(user.email, include_cancelled, now_ms)
    if calendar_events is None:
        raise HTTPException(status_code=502, detail="Calendar events store (D1) unavailable")

    m_result = await db.execute(
        select(Meeting).where(Meeting.user_id == user.id).order_by(Meeting.created_at.desc())
    )
    meetings = m_result.scalars().all()

    linked_meeting_ids = {e["meeting_id"] for e in calendar_events if e.get("meeting_id")}

    return {
        "email": email,
        "user_id": user.id,
        "calendar_events": calendar_events,
        "meetings": [
            {
                "id": m.id,
                "platform": m.platform,
                "native_meeting_id": m.platform_specific_id,
                "meeting_url": _meet_url(m.platform, m.platform_specific_id),
                "status": m.status,
                "start_time": m.start_time.isoformat() if m.start_time else None,
                "end_time": m.end_time.isoformat() if m.end_time else None,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "source": "calendar" if m.id in linked_meeting_ids else "manual",
            }
            for m in meetings
        ],
    }


class CalendarMeetingRemoveRequest(BaseModel):
    email: str
    event_id: int


@app.post("/calendar/meetings/remove")
async def remove_client_meeting(
    body: CalendarMeetingRemoveRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
):
    """Remove a scheduled/upcoming meeting for a client (server-to-server).

    Identify the client by `email` and the meeting by `event_id` (the `id` from
    GET /calendar/meetings' calendar_events array). The event is marked
    'cancelled' so the bot won't auto-join. If a bot was already dispatched and
    is still live, it's asked to leave.

    We CANCEL rather than DELETE the row on purpose: the 5-min sync re-polls
    Google and re-inserts events, so a hard delete would reappear as 'pending'
    on the next poll and get re-joined. The sync upsert deliberately never
    overwrites `status`, so 'cancelled' sticks. See app/sync.py.
    """
    _check_system_key(x_api_key)
    email = (body.email or "").strip().lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Client not found")

    ev_result = await db.execute(
        select(CalendarEvent).where(
            CalendarEvent.id == body.event_id,
            CalendarEvent.user_id == user.id,
        )
    )
    event = ev_result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Meeting not found for this client")

    previous_status = event.status

    # If a bot was already dispatched for this event, ask it to leave. Best-effort:
    # a 404/already-stopped from meeting-api is fine — we still cancel the event.
    # Same trusted-identity contract the sync loop uses to LAUNCH bots (sync.py).
    bot_stopped = False
    if event.meeting_id and previous_status == "scheduled" and event.meeting_url and event.platform:
        try:
            native_id = _extract_native_id(event.meeting_url, event.platform)
            headers = {
                "X-User-ID": str(event.user_id),
                "X-User-Scopes": "bot",
                "X-User-Limits": str(getattr(user, "max_concurrent_bots", 1) or 1),
            }
            if BOT_API_TOKEN:
                headers["X-API-Key"] = BOT_API_TOKEN
            async with httpx.AsyncClient() as client:
                resp = await client.request(
                    "DELETE",
                    f"{MEETING_API_URL}/bots/{event.platform}/{native_id}",
                    headers=headers,
                    timeout=30,
                )
            bot_stopped = resp.status_code in (200, 202)
            if not bot_stopped:
                logger.warning(
                    f"Stop-bot for event {event.id} returned {resp.status_code}: {resp.text}"
                )
        except Exception as e:
            logger.error(f"Failed to stop bot for event {event.id}: {e}")

    await db.execute(
        update(CalendarEvent).where(CalendarEvent.id == event.id).values(status="cancelled")
    )
    await db.commit()

    # This flips status directly, bypassing sync.py's per-event mirror — keep
    # D1 reconciled here too (best-effort, never raises).
    await mirror_calendar_event_to_d1(
        event_id=event.id,
        owner_email=user.email,
        title=event.title,
        start_time=event.start_time,
        end_time=event.end_time,
        meeting_url=event.meeting_url,
        platform=event.platform,
        status="cancelled",
        meeting_id=event.meeting_id,
    )

    return {
        "status": "removed",
        "event_id": event.id,
        "previous_status": previous_status,
        "bot_stopped": bot_stopped,
    }


class CalendarMeetingRestoreRequest(BaseModel):
    email: str
    event_id: int


@app.post("/calendar/meetings/restore")
async def restore_client_meeting(
    body: CalendarMeetingRestoreRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
):
    """Un-remove a previously removed meeting (server-to-server).

    Flips a 'cancelled' event back to 'pending' so the scheduler will auto-join
    it again when it comes within lead time. Find cancelled events to restore
    via GET /calendar/meetings?include_cancelled=true.

    No-op (not an error) if the event isn't currently cancelled — the response
    reports its actual status so the caller can react.
    """
    _check_system_key(x_api_key)
    email = (body.email or "").strip().lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Client not found")

    ev_result = await db.execute(
        select(CalendarEvent).where(
            CalendarEvent.id == body.event_id,
            CalendarEvent.user_id == user.id,
        )
    )
    event = ev_result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Meeting not found for this client")

    if event.status != "cancelled":
        # Nothing to undo — report current state rather than silently flipping a
        # live/scheduled meeting back to pending.
        return {
            "status": "noop",
            "event_id": event.id,
            "current_status": event.status,
            "detail": "Meeting is not removed; nothing to restore.",
        }

    await db.execute(
        update(CalendarEvent).where(CalendarEvent.id == event.id).values(status="pending")
    )
    await db.commit()

    # Same reasoning as remove_client_meeting above — mirror the flip to D1.
    await mirror_calendar_event_to_d1(
        event_id=event.id,
        owner_email=user.email,
        title=event.title,
        start_time=event.start_time,
        end_time=event.end_time,
        meeting_url=event.meeting_url,
        platform=event.platform,
        status="pending",
        meeting_id=event.meeting_id,
    )

    return {
        "status": "restored",
        "event_id": event.id,
        "new_status": "pending",
    }


class CalendarUnsubscribeRequest(BaseModel):
    email: str


@app.post("/calendar/unsubscribe")
async def unsubscribe_client(
    body: CalendarUnsubscribeRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
):
    """Fully unsubscribe a client by email (server-to-server): cancel every
    non-cancelled calendar event (stopping any already-dispatched live bot
    along the way), remove every D1 `schedules` row they own, and disconnect
    their Google Calendar. After this call nothing auto-joins on their
    behalf again until they reconnect their calendar or add a new schedule.
    """
    _check_system_key(x_api_key)
    email = (body.email or "").strip().lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Client not found")

    ev_result = await db.execute(
        select(CalendarEvent).where(
            CalendarEvent.user_id == user.id,
            CalendarEvent.status != "cancelled",
        )
    )
    events = ev_result.scalars().all()

    bots_stopped = 0
    for event in events:
        # Same stop-then-cancel contract as remove_client_meeting: only a
        # dispatched ("scheduled") event can have a live bot to stop.
        if event.meeting_id and event.status == "scheduled" and event.meeting_url and event.platform:
            try:
                native_id = _extract_native_id(event.meeting_url, event.platform)
                headers = {
                    "X-User-ID": str(event.user_id),
                    "X-User-Scopes": "bot",
                    "X-User-Limits": str(getattr(user, "max_concurrent_bots", 1) or 1),
                }
                if BOT_API_TOKEN:
                    headers["X-API-Key"] = BOT_API_TOKEN
                async with httpx.AsyncClient() as client:
                    resp = await client.request(
                        "DELETE",
                        f"{MEETING_API_URL}/bots/{event.platform}/{native_id}",
                        headers=headers,
                        timeout=30,
                    )
                if resp.status_code in (200, 202):
                    bots_stopped += 1
                else:
                    logger.warning(
                        f"Unsubscribe {email}: stop-bot for event {event.id} "
                        f"returned {resp.status_code}: {resp.text}"
                    )
            except Exception as e:
                logger.error(f"Unsubscribe {email}: failed to stop bot for event {event.id}: {e}")

        await db.execute(
            update(CalendarEvent).where(CalendarEvent.id == event.id).values(status="cancelled")
        )
        await mirror_calendar_event_to_d1(
            event_id=event.id,
            owner_email=email,
            title=event.title,
            start_time=event.start_time,
            end_time=event.end_time,
            meeting_url=event.meeting_url,
            platform=event.platform,
            status="cancelled",
            meeting_id=event.meeting_id,
        )

    await db.commit()

    schedules_removed = await delete_schedules_by_owner(email)

    user_data = dict(user.data or {})
    had_calendar = "google_calendar" in user_data
    user_data.pop("google_calendar", None)
    await db.execute(update(User).where(User.id == user.id).values(data=user_data))
    await db.commit()

    return {
        "status": "unsubscribed",
        "email": email,
        "calendar_events_cancelled": len(events),
        "bots_stopped": bots_stopped,
        "schedules_removed": schedules_removed,
        "calendar_disconnected": had_calendar,
    }


@app.post("/calendar/connect")
async def connect_calendar(user_id: int = Query(...), db: AsyncSession = Depends(get_db)):
    """Trigger initial sync after OAuth connection."""
    try:
        count = await sync_user_calendar(user_id, db)
    except (CalendarTokenRevoked, CalendarScopeInsufficient):
        # Grant is dead or carries no calendar scope; sync_user_calendar has
        # already dropped it. Report disconnected rather than a raw 500 — the
        # caller's remedy is the same either way: send the user through consent.
        connect_url = f"{PUBLIC_BASE_URL}/calendar/connect/start" if PUBLIC_BASE_URL else None
        return {
            "status": "disconnected",
            "events_synced": 0,
            "connect_url": connect_url,
            "detail": "Calendar access not granted — reconnect required.",
        }
    return {"status": "connected", "events_synced": count}


@app.get("/calendar/status")
async def calendar_status(user_id: int = Query(...), db: AsyncSession = Depends(get_db)):
    """Check if user has calendar connected."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    gc = (user.data or {}).get("google_calendar", {})
    connected = bool(gc.get("oauth", {}).get("refresh_token"))

    event_count = 0
    if connected:
        count_result = await db.execute(
            select(CalendarEvent).where(CalendarEvent.user_id == user_id)
        )
        event_count = len(count_result.scalars().all())

    return {
        "connected": connected,
        "event_count": event_count,
    }


@app.delete("/calendar/disconnect")
async def disconnect_calendar(user_id: int = Query(...), db: AsyncSession = Depends(get_db)):
    """Remove OAuth tokens and stop syncing."""
    from sqlalchemy import update
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user_data = dict(user.data or {})
    user_data.pop("google_calendar", None)
    await db.execute(
        update(User).where(User.id == user_id).values(data=user_data)
    )
    await db.commit()
    return {"status": "disconnected"}


@app.get("/calendar/events")
async def list_events(user_id: int = Query(...), db: AsyncSession = Depends(get_db)):
    """List upcoming calendar events for a user."""
    result = await db.execute(
        select(CalendarEvent)
        .where(CalendarEvent.user_id == user_id)
        .order_by(CalendarEvent.start_time)
    )
    events = result.scalars().all()
    return [
        {
            "id": e.id,
            "title": e.title,
            "start_time": e.start_time.isoformat() if e.start_time else None,
            "end_time": e.end_time.isoformat() if e.end_time else None,
            "meeting_url": e.meeting_url,
            "platform": e.platform,
            "status": e.status,
        }
        for e in events
    ]


@app.put("/calendar/preferences")
async def update_preferences(
    user_id: int = Query(...),
    auto_join: bool = True,
    lead_time_minutes: int = 2,
    db: AsyncSession = Depends(get_db),
):
    """Set auto-join and lead time preferences."""
    from sqlalchemy import update
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user_data = dict(user.data or {})
    gc = user_data.get("google_calendar", {})
    gc["preferences"] = {
        "auto_join": auto_join,
        "lead_time_minutes": lead_time_minutes,
    }
    user_data["google_calendar"] = gc
    await db.execute(
        update(User).where(User.id == user_id).values(data=user_data)
    )
    await db.commit()
    return {"status": "updated", "preferences": gc["preferences"]}


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8050, reload=True)
