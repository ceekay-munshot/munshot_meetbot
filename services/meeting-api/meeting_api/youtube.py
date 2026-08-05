"""YouTube transcript endpoints (authenticated, per-user).

  POST /youtube/transcripts        — queue a public YouTube URL, returns 202 + id
  GET  /youtube/transcripts        — list the caller's transcripts
  GET  /youtube/transcripts/{id}   — one transcript: metadata + segments + text
  GET  /youtube/transcripts/{id}.txt — the same transcript as plain text

Async by design: a 1-hour video is ~1-3 min end to end on the ASR path, well past
what a proxy will hold open, so the POST queues the job and returns immediately.
Poll the GET until `status` is `completed` (or `failed`, which carries `error`).

Ownership is enforced on every read — a transcript belongs to the user_id that
created it, and the public gateway route resolves that user from the caller's
email exactly like /public/join does.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from .auth import UserProxy
from .collector.auth import get_current_user
from .database import get_db
from .models import YouTubeTranscript, YouTubeTranscriptSegment
from . import youtube_transcribe

logger = logging.getLogger("meeting_api.youtube")

router = APIRouter(prefix="/youtube", tags=["YouTube"])


class YouTubeTranscriptRequest(BaseModel):
    url: str = Field(
        ...,
        description="Public YouTube URL or bare 11-char video id. watch?v=, youtu.be/, "
                    "/shorts/, /live/ and /embed/ forms are all accepted.",
        examples=["https://www.youtube.com/watch?v=aircAruvnKk"],
    )
    force: bool = Field(
        False,
        description="Re-transcribe even if this video was already transcribed for this user.",
    )


class YouTubeSegmentResponse(BaseModel):
    index: int
    start: float
    end: float
    text: str
    speaker: Optional[str] = None
    language: Optional[str] = None


class YouTubeTranscriptResponse(BaseModel):
    id: int
    video_id: str
    url: str
    title: Optional[str] = None
    channel: Optional[str] = None
    duration_seconds: Optional[int] = None
    status: str = Field(..., description="queued | processing | completed | failed")
    source: Optional[str] = Field(None, description="captions | deepgram")
    language: Optional[str] = None
    segment_count: int = 0
    error: Optional[str] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None
    text: Optional[str] = Field(None, description="Full transcript; null until completed.")
    segments: List[YouTubeSegmentResponse] = []


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _meta_response(row: YouTubeTranscript) -> dict:
    return {
        "id": row.id,
        "video_id": row.video_id,
        "url": row.url,
        "title": row.title,
        "channel": row.channel,
        "duration_seconds": row.duration_seconds,
        "status": row.status,
        "source": row.source,
        "language": row.language,
        # Coerced: the column's default is a server_default, so a freshly added
        # row reads back None here until Postgres has applied it.
        "segment_count": row.segment_count or 0,
        "error": row.error,
        "created_at": _iso(row.created_at),
        "completed_at": _iso(row.completed_at),
    }


async def _load_owned(transcript_id: int, user_id: int, db) -> YouTubeTranscript:
    row = await db.get(YouTubeTranscript, transcript_id)
    # 404 rather than 403 on someone else's transcript: a wrong-owner 403 would
    # confirm the id exists.
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail=f"Transcript not found: {transcript_id}")
    return row


@router.post(
    "/transcripts",
    response_model=YouTubeTranscriptResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a public YouTube video for transcription",
    description=(
        "Accepts a public YouTube link and returns immediately with a transcript id "
        "in `queued` status. The job tries the video's own captions first (free) and "
        "falls back to Deepgram ASR on the audio when there are none. Poll "
        "GET /youtube/transcripts/{id} until status is `completed`.\n\n"
        "Re-posting a URL already transcribed for this user returns the existing "
        "transcript untouched unless `force` is set."
    ),
)
async def create_youtube_transcript(
    body: YouTubeTranscriptRequest,
    current_user: UserProxy = Depends(get_current_user),
    db=Depends(get_db),
):
    try:
        video_id = youtube_transcribe.extract_video_id(body.url)
    except youtube_transcribe.YouTubeError as e:
        raise HTTPException(status_code=422, detail=str(e))

    existing = (await db.execute(
        select(YouTubeTranscript).where(
            YouTubeTranscript.user_id == current_user.id,
            YouTubeTranscript.video_id == video_id,
        )
    )).scalar_one_or_none()

    if existing is not None and not body.force:
        # Idempotent by (user, video). A failed job is worth retrying though —
        # otherwise a transient YouTube error would wedge that video forever.
        if existing.status != "failed":
            return YouTubeTranscriptResponse(**_meta_response(existing), segments=[])
        logger.info(f"youtube: retrying previously failed transcript {existing.id}")

    if existing is not None:
        row = existing
        row.status = "queued"
        row.error = None
        row.completed_at = None
    else:
        row = YouTubeTranscript(
            user_id=current_user.id,
            video_id=video_id,
            url=youtube_transcribe.canonical_url(video_id),
            status="queued",
        )
        db.add(row)
    await db.commit()
    await db.refresh(row)

    youtube_transcribe.spawn_job(row.id)
    logger.info(
        f"youtube: queued transcript {row.id} for video {video_id} (user {current_user.id})"
    )
    return YouTubeTranscriptResponse(**_meta_response(row), segments=[])


@router.get(
    "/transcripts",
    response_model=List[YouTubeTranscriptResponse],
    summary="List the caller's YouTube transcripts (metadata only)",
)
async def list_youtube_transcripts(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: Optional[str] = Query(None, alias="status"),
    current_user: UserProxy = Depends(get_current_user),
    db=Depends(get_db),
):
    stmt = select(YouTubeTranscript).where(YouTubeTranscript.user_id == current_user.id)
    if status_filter:
        stmt = stmt.where(YouTubeTranscript.status == status_filter)
    stmt = stmt.order_by(YouTubeTranscript.created_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    return [YouTubeTranscriptResponse(**_meta_response(r), segments=[]) for r in rows]


async def _segments_for(transcript_id: int, db) -> List[YouTubeTranscriptSegment]:
    return list((await db.execute(
        select(YouTubeTranscriptSegment)
        .where(YouTubeTranscriptSegment.transcript_id == transcript_id)
        .order_by(YouTubeTranscriptSegment.segment_index)
    )).scalars().all())


# Declared BEFORE /transcripts/{transcript_id}: FastAPI matches routes in
# declaration order, so with the int-typed route first, "/transcripts/5.txt" would
# try int("5.txt") and 422 before ever reaching this handler.
@router.get(
    "/transcripts/{transcript_id}.txt",
    summary="Get one YouTube transcript as plain text",
    response_class=Response,
)
async def get_youtube_transcript_text(
    transcript_id: int,
    current_user: UserProxy = Depends(get_current_user),
    db=Depends(get_db),
):
    row = await _load_owned(transcript_id, current_user.id, db)
    if row.status != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"Transcript is {row.status}" + (f": {row.error}" if row.error else ""),
        )
    segments = await _segments_for(transcript_id, db)
    return Response(
        content="\n".join(s.text for s in segments),
        media_type="text/plain; charset=utf-8",
    )


@router.get(
    "/transcripts/{transcript_id}",
    response_model=YouTubeTranscriptResponse,
    summary="Get one YouTube transcript with its segments and full text",
)
async def get_youtube_transcript(
    transcript_id: int,
    current_user: UserProxy = Depends(get_current_user),
    db=Depends(get_db),
):
    row = await _load_owned(transcript_id, current_user.id, db)
    segments = await _segments_for(transcript_id, db)
    return YouTubeTranscriptResponse(
        **_meta_response(row),
        # Assembled on read rather than stored: keeps one copy of the text in the
        # source of truth (see the YouTubeTranscript docstring).
        text="\n".join(s.text for s in segments) or None,
        segments=[
            YouTubeSegmentResponse(
                index=s.segment_index, start=s.start_time, end=s.end_time,
                text=s.text, speaker=s.speaker, language=s.language,
            )
            for s in segments
        ],
    )
