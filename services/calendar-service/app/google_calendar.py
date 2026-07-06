"""Google Calendar API client — auth, event listing, meeting URL extraction."""

import re
import json
import base64
import logging
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("calendar-service.google")

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"

# openid+email so we can identify the connecting account from the id_token
# (never trust a client-supplied email); calendar.readonly for sync.
OAUTH_SCOPES = "openid email https://www.googleapis.com/auth/calendar.readonly"


def build_consent_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the Google OAuth consent URL (offline access, forced consent)."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": OAUTH_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


async def exchange_code_for_tokens(
    client_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict:
    """Exchange an authorization code for tokens (refresh_token + id_token)."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
        )
        resp.raise_for_status()
        return resp.json()


def email_from_id_token(id_token: str) -> Optional[str]:
    """Extract the verified email from a Google id_token JWT payload.

    No signature verification needed: the token came directly from Google's
    token endpoint over TLS, so its contents are trusted.
    """
    try:
        payload_b64 = id_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("email")
    except Exception as e:
        logger.warning(f"Could not decode id_token email: {e}")
        return None


async def refresh_access_token(
    client_id: str, client_secret: str, refresh_token: str
) -> tuple[str, int]:
    """Exchange a refresh token for a fresh access token. Returns (access_token, expires_in)."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["access_token"], int(data.get("expires_in", 3600))


async def list_events(
    access_token: str,
    time_min: Optional[datetime] = None,
    time_max: Optional[datetime] = None,
    sync_token: Optional[str] = None,
    max_results: int = 250,
    page_token: Optional[str] = None,
) -> dict:
    """Fetch one page of events from Google Calendar API. Returns raw API response dict.

    Callers wanting the full window must follow ``nextPageToken`` themselves
    (see sync.py's ``list_all_events``) — Google paginates at ``max_results``
    per call regardless of how wide time_min/time_max is.
    """
    params: dict[str, str] = {
        "maxResults": str(max_results),
        "singleEvents": "true",
        "orderBy": "startTime",
    }

    if sync_token:
        params["syncToken"] = sync_token
    else:
        if time_min:
            params["timeMin"] = time_min.isoformat()
        if time_max:
            params["timeMax"] = time_max.isoformat()

    if page_token:
        params["pageToken"] = page_token

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            EVENTS_URL,
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code == 410:
            # Sync token expired — caller should do a full sync
            return {"items": [], "nextSyncToken": None, "fullSyncRequired": True}
        resp.raise_for_status()
        return resp.json()


# --- Meeting URL extraction ---

MEETING_URL_PATTERNS = [
    re.compile(r"https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}"),
    re.compile(r"https://[\w.-]*zoom\.us/j/\d+(\?pwd=\w+)?"),
    re.compile(r"https://teams\.microsoft\.com/l/meetup-join/[^\s\"<>]+"),
]


def extract_meeting_url(event: dict) -> Optional[str]:
    """Extract a meeting URL from a Google Calendar event object."""
    # 1. conferenceData.entryPoints (most reliable)
    conference_data = event.get("conferenceData", {})
    for ep in conference_data.get("entryPoints", []):
        if ep.get("entryPointType") == "video" and ep.get("uri"):
            return ep["uri"]

    # 2. hangoutLink
    hangout = event.get("hangoutLink")
    if hangout:
        return hangout

    # 3. Scan location and description for known patterns
    for field in ["location", "description"]:
        text = event.get(field, "") or ""
        for pattern in MEETING_URL_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(0)

    return None


def detect_platform(url: str) -> Optional[str]:
    """Detect the meeting platform from a URL."""
    if "meet.google.com" in url:
        return "google_meet"
    if "zoom.us" in url:
        return "zoom"
    if "teams.microsoft.com" in url:
        return "teams"
    return None


def parse_event_time(event: dict, key: str) -> Optional[datetime]:
    """Parse start or end time from a Google Calendar event."""
    time_info = event.get(key, {})
    dt_str = time_info.get("dateTime")
    if dt_str:
        return datetime.fromisoformat(dt_str)
    # All-day events only have 'date', skip them (no meeting time)
    return None
