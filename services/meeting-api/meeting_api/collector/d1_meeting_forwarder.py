"""Mirror meeting-level state into a Cloudflare D1 database.

Companion to ``d1_forwarder.py`` (which mirrors transcript segments). This
sink mirrors meeting metadata + lifecycle state so the Cloudflare side can
answer "what meetings exist for a user, what is their status, basic
metadata, terminal outcome" without round-tripping to AWS for every read.

Key rules (per implement-this.txt):
  * Postgres remains the source of truth. D1 is a best-effort mirror.
  * Sink failures NEVER raise into the caller — meeting creation,
    callbacks, post-meeting tasks must not break if D1 is unreachable.
  * Secrets are NOT mirrored. ``webhook_url``, ``webhook_secret``,
    ``webhook_events`` from ``meeting.data`` are explicitly excluded.
  * Idempotent upsert keyed on ``meeting_id``.

Enable via ``CLOUDFLARE_D1_ENABLED=true`` + the CF_* credentials
(see config.py). Schema: deploy/cloudflare-d1/schema_meetings.sql.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import httpx

from .config import (
    CLOUDFLARE_D1_ENABLED,
    CF_ACCOUNT_ID,
    CF_D1_DATABASE_ID,
    CF_API_TOKEN,
    CF_D1_MEETINGS_TABLE,
    CF_D1_TIMEOUT_SECONDS,
)
from ..models import Meeting

logger = logging.getLogger(__name__)


def _d1_query_url() -> str:
    return (
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
        f"/d1/database/{CF_D1_DATABASE_ID}/query"
    )


def _is_configured() -> bool:
    if not CLOUDFLARE_D1_ENABLED:
        return False
    missing = [
        name
        for name, val in (
            ("CF_ACCOUNT_ID", CF_ACCOUNT_ID),
            ("CF_D1_DATABASE_ID", CF_D1_DATABASE_ID),
            ("CF_API_TOKEN", CF_API_TOKEN),
        )
        if not val
    ]
    if missing:
        logger.warning(
            "Cloudflare D1 meeting mirror enabled but missing config: %s. Skipping.",
            ", ".join(missing),
        )
        return False
    return True


_COLUMNS = (
    "meeting_id",
    "user_id",
    "platform",
    "native_meeting_id",
    "status",
    "bot_name",
    "language",
    "transcribe_enabled",
    "recording_enabled",
    "segment_count",
    "started_at",
    "ended_at",
    "created_at",
    "updated_at",
    "completion_reason",
    "failure_stage",
)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _bool_to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return 1 if bool(value) else 0


def build_snapshot(meeting: Meeting) -> dict:
    """Project a Meeting ORM row into the D1 mirror shape.

    Pulls typed fields from the row, hoists analytics fields from JSONB
    data, and drops anything sensitive. Returns a dict whose keys are
    the exact D1 columns; values may be None.
    """
    data = meeting.data if isinstance(meeting.data, dict) else {}
    return {
        "meeting_id": meeting.id,
        "user_id": meeting.user_id,
        "platform": meeting.platform,
        "native_meeting_id": meeting.platform_specific_id,
        "status": meeting.status,
        "bot_name": data.get("bot_name"),
        "language": data.get("language"),
        "transcribe_enabled": _bool_to_int(data.get("transcribe_enabled")),
        "recording_enabled": _bool_to_int(data.get("recording_enabled")),
        "segment_count": data.get("segment_count"),
        "started_at": _iso(meeting.start_time),
        "ended_at": _iso(meeting.end_time),
        "created_at": _iso(meeting.created_at),
        "updated_at": _iso(meeting.updated_at),
        "completion_reason": data.get("completion_reason"),
        "failure_stage": data.get("failure_stage"),
    }


def _p(value: Any) -> Optional[str]:
    """D1's /query params are documented as strings; SQLite affinity coerces
    them back to INTEGER on insert. NULL stays as JSON null."""
    return None if value is None else str(value)


def _build_upsert(snapshot: dict) -> dict:
    placeholders = ", ".join(["?"] * len(_COLUMNS))
    cols = ", ".join(_COLUMNS)
    # On conflict, refresh every column EXCEPT meeting_id (the key) and
    # created_at (immutable once mirrored — Postgres owns it). Allows late
    # status updates to overwrite earlier snapshots.
    update_cols = [c for c in _COLUMNS if c not in ("meeting_id", "created_at")]
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
    sql = (
        f"INSERT INTO {CF_D1_MEETINGS_TABLE} ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT (meeting_id) DO UPDATE SET {set_clause}"
    )
    params = [_p(snapshot[c]) for c in _COLUMNS]
    return {"sql": sql, "params": params}


async def forward_meeting_to_d1(meeting: Meeting) -> None:
    """Best-effort meeting-state mirror to Cloudflare D1.

    Catches all errors internally and logs them; never raises.
    Skips silently when D1 is not configured or the meeting has no id.
    """
    if not _is_configured():
        return
    if meeting is None or getattr(meeting, "id", None) is None:
        return

    try:
        snapshot = build_snapshot(meeting)
    except Exception as e:  # noqa: BLE001 - sink must never propagate
        logger.error("D1 meeting snapshot build failed (non-fatal): %s", e, exc_info=True)
        return

    chunk = _build_upsert(snapshot)
    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json",
    }
    url = _d1_query_url()

    try:
        async with httpx.AsyncClient(timeout=CF_D1_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, headers=headers, json=chunk)
            if resp.status_code != 200:
                logger.error(
                    "D1 meeting forward HTTP %s: %s",
                    resp.status_code, resp.text[:500],
                )
                return
            body = resp.json()
            if not body.get("success", False):
                logger.error("D1 meeting forward returned errors: %s", body.get("errors"))
                return
        logger.info("Mirrored meeting %s snapshot to Cloudflare D1", snapshot["meeting_id"])
    except httpx.RequestError as e:
        logger.error("D1 meeting forward request error (non-fatal): %s", e)
    except Exception as e:  # noqa: BLE001 - sink must never propagate
        logger.error("D1 meeting forward unexpected error (non-fatal): %s", e, exc_info=True)


async def safe_mirror_meeting(meeting: Meeting) -> None:
    """Convenience wrapper for lifecycle call sites.

    Identical to ``forward_meeting_to_d1`` today; kept as a named entry
    point so future fan-out (e.g. batching, queueing) stays a one-line
    change at call sites.
    """
    await forward_meeting_to_d1(meeting)
