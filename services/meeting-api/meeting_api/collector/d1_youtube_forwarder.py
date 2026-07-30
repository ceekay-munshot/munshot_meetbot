"""Mirror YouTube transcripts into Cloudflare D1 for frontend reads.

Companion to ``d1_forwarder.py`` (meeting transcript segments) and
``d1_meeting_forwarder.py`` (meeting state). Same contract as both:

  * Postgres stays the source of truth; D1 is a best-effort read mirror.
  * This sink NEVER raises into the caller — a D1 outage must not fail a job.
  * ``owner_email`` is carried on every row so the Cloudflare Worker can filter a
    client's transcripts in D1 without round-tripping to AWS.

Two tables, mirroring the Postgres split: one metadata row per video plus its
timed segments. The full transcript text is deliberately NOT denormalised onto
the metadata row — a 3-hour video's text would be a single ~200 KB bound
parameter, and the frontend already assembles meeting transcripts from segment
rows, so it can do the same here.

Schema: deploy/cloudflare-d1/schema_youtube_transcripts.sql
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, List, Optional

import httpx
from sqlalchemy import select

from .config import (
    CLOUDFLARE_D1_ENABLED,
    CF_ACCOUNT_ID,
    CF_D1_DATABASE_ID,
    CF_API_TOKEN,
    CF_D1_YOUTUBE_TABLE,
    CF_D1_YOUTUBE_SEGMENTS_TABLE,
    CF_D1_TIMEOUT_SECONDS,
)
from ..database import async_session_local
from ..models import YouTubeTranscript, YouTubeTranscriptSegment

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
            "Cloudflare D1 YouTube mirror enabled but missing config: %s. Skipping.",
            ", ".join(missing),
        )
        return False
    return True


_META_COLUMNS = (
    "transcript_id",
    "user_id",
    "owner_email",
    "video_id",
    "url",
    "title",
    "channel",
    "duration_seconds",
    "status",
    "source",
    "language",
    "segment_count",
    "error",
    "created_at",
    "updated_at",
    "completed_at",
)

_SEGMENT_COLUMNS = (
    "transcript_id",
    "segment_index",
    "start_time",
    "end_time",
    "text",
    "speaker",
    "language",
    "owner_email",
)

# D1 caps a single statement at 100 BOUND PARAMETERS (not SQLite's 999) — exceed
# it and D1 returns 7500 "too many SQL variables". Same margin as d1_forwarder.
_ROWS_PER_CHUNK = max(1, 90 // len(_SEGMENT_COLUMNS))


def _p(value: Any) -> Optional[str]:
    """D1's /query params are documented as strings; SQLite affinity coerces them
    back to INTEGER/REAL on insert. NULL stays as JSON null."""
    return None if value is None else str(value)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


async def _resolve_owner_email(db, user_id: int) -> Optional[str]:
    """user_id -> email, for the mirror's ownership column.

    Best-effort: any DB/import error mirrors with a NULL owner_email rather than
    failing the transcript write (same posture as db_writer._resolve_owner_emails).
    """
    try:
        from admin_models.models import User
        return (await db.execute(select(User.email).where(User.id == user_id))).scalar_one_or_none()
    except Exception as e:  # noqa: BLE001
        logger.error("Could not resolve owner email for YouTube D1 mirror (non-fatal): %s", e)
        return None


def build_meta_snapshot(row: YouTubeTranscript, owner_email: Optional[str]) -> dict:
    return {
        "transcript_id": row.id,
        "user_id": row.user_id,
        "owner_email": owner_email,
        "video_id": row.video_id,
        "url": row.url,
        "title": row.title,
        "channel": row.channel,
        "duration_seconds": row.duration_seconds,
        "status": row.status,
        "source": row.source,
        "language": row.language,
        "segment_count": row.segment_count,
        "error": row.error,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "completed_at": _iso(row.completed_at),
    }


def _build_meta_upsert(snapshot: dict) -> dict:
    cols = ", ".join(_META_COLUMNS)
    placeholders = ", ".join(["?"] * len(_META_COLUMNS))
    # Refresh everything except the key and created_at (immutable once mirrored),
    # so a later status change overwrites an earlier snapshot.
    update_cols = [c for c in _META_COLUMNS if c not in ("transcript_id", "created_at")]
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
    return {
        "sql": (
            f"INSERT INTO {CF_D1_YOUTUBE_TABLE} ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT (transcript_id) DO UPDATE SET {set_clause}"
        ),
        "params": [_p(snapshot[c]) for c in _META_COLUMNS],
    }


def _build_segment_chunks(
    segments: List[YouTubeTranscriptSegment], owner_email: Optional[str]
) -> List[dict]:
    cols = ", ".join(_SEGMENT_COLUMNS)
    placeholder = "(" + ", ".join(["?"] * len(_SEGMENT_COLUMNS)) + ")"
    update_cols = [c for c in _SEGMENT_COLUMNS if c not in ("transcript_id", "segment_index")]
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in update_cols)

    chunks: List[dict] = []
    for i in range(0, len(segments), _ROWS_PER_CHUNK):
        rows = segments[i : i + _ROWS_PER_CHUNK]
        values = ", ".join([placeholder] * len(rows))
        params: List[Optional[str]] = []
        for s in rows:
            params.extend([
                _p(s.transcript_id), _p(s.segment_index), _p(s.start_time),
                _p(s.end_time), _p(s.text), _p(s.speaker), _p(s.language),
                _p(owner_email),
            ])
        chunks.append({
            "sql": (
                f"INSERT INTO {CF_D1_YOUTUBE_SEGMENTS_TABLE} ({cols}) VALUES {values} "
                f"ON CONFLICT (transcript_id, segment_index) DO UPDATE SET {set_clause}"
            ),
            "params": params,
        })
    return chunks


async def _send(client: httpx.AsyncClient, headers: dict, chunk: dict) -> bool:
    resp = await client.post(_d1_query_url(), headers=headers, json=chunk)
    if resp.status_code != 200:
        logger.error("D1 YouTube forward HTTP %s: %s", resp.status_code, resp.text[:500])
        return False
    body = resp.json()
    if not body.get("success", False):
        logger.error("D1 YouTube forward returned errors: %s", body.get("errors"))
        return False
    return True


async def forward_youtube_transcript_to_d1(transcript_id: int) -> None:
    """Mirror one transcript (metadata + all segments) to D1. Never raises."""
    if not _is_configured():
        return

    async with async_session_local() as db:
        row = await db.get(YouTubeTranscript, transcript_id)
        if row is None:
            return
        owner_email = await _resolve_owner_email(db, row.user_id)
        snapshot = build_meta_snapshot(row, owner_email)
        segments = list((await db.execute(
            select(YouTubeTranscriptSegment)
            .where(YouTubeTranscriptSegment.transcript_id == transcript_id)
            .order_by(YouTubeTranscriptSegment.segment_index)
        )).scalars().all())

    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=CF_D1_TIMEOUT_SECONDS) as client:
            # Metadata first: if the segment writes fail halfway, the frontend at
            # least sees the row with its real status rather than nothing.
            if not await _send(client, headers, _build_meta_upsert(snapshot)):
                return
            sent = 0
            for chunk in _build_segment_chunks(segments, owner_email):
                if not await _send(client, headers, chunk):
                    logger.error(
                        "D1 YouTube segment mirror aborted for transcript %s after %s/%s rows",
                        transcript_id, sent, len(segments),
                    )
                    return
                sent += len(chunk["params"]) // len(_SEGMENT_COLUMNS)
        logger.info(
            "Mirrored YouTube transcript %s (%s segments) to Cloudflare D1",
            transcript_id, len(segments),
        )
    except httpx.RequestError as e:
        logger.error("D1 YouTube forward request error (non-fatal): %s", e)
    except Exception as e:  # noqa: BLE001 - sink must never propagate
        logger.error("D1 YouTube forward unexpected error (non-fatal): %s", e, exc_info=True)


async def safe_mirror_youtube_transcript(transcript_id: int) -> None:
    """Named entry point for job call sites (see d1_meeting_forwarder.safe_mirror_meeting)."""
    await forward_youtube_transcript_to_d1(transcript_id)
