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
from sqlalchemy import select, update, or_, and_

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
from app.google_calendar import (
    build_consent_url,
    exchange_code_for_tokens,
    email_from_id_token,
    CalendarTokenRevoked,
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
    # when an event has no end.
    now = datetime.now(timezone.utc)
    conditions = [
        CalendarEvent.user_id == user.id,
        or_(
            CalendarEvent.end_time >= now,
            and_(CalendarEvent.end_time.is_(None), CalendarEvent.start_time >= now),
        ),
    ]
    # Hide removed meetings by default; include them (with status='cancelled')
    # only when the caller explicitly asks — e.g. to offer an "un-remove".
    if not include_cancelled:
        conditions.append(CalendarEvent.status != "cancelled")

    ev_result = await db.execute(
        select(CalendarEvent).where(*conditions).order_by(CalendarEvent.start_time)
    )
    events = ev_result.scalars().all()

    m_result = await db.execute(
        select(Meeting).where(Meeting.user_id == user.id).order_by(Meeting.created_at.desc())
    )
    meetings = m_result.scalars().all()

    linked_meeting_ids = {e.meeting_id for e in events if e.meeting_id}

    return {
        "email": email,
        "user_id": user.id,
        "calendar_events": [
            {
                "id": e.id,
                "title": e.title,
                "start_time": e.start_time.isoformat() if e.start_time else None,
                "end_time": e.end_time.isoformat() if e.end_time else None,
                "meeting_url": e.meeting_url,
                "platform": e.platform,
                "status": e.status,
                "meeting_id": e.meeting_id,
            }
            for e in events
        ],
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

    return {
        "status": "restored",
        "event_id": event.id,
        "new_status": "pending",
    }


@app.post("/calendar/connect")
async def connect_calendar(user_id: int = Query(...), db: AsyncSession = Depends(get_db)):
    """Trigger initial sync after OAuth connection."""
    count = await sync_user_calendar(user_id, db)
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
