"""Internal recording-ingest endpoints (bot -> meeting-api).

Two bot-facing endpoints, both authenticated with the per-meeting MeetingToken
(the same HS256 JWT the bot already holds as ``token``, sent as
``Authorization: Bearer <token>``):

  POST /internal/recordings/chunk            — one audio chunk per timeslice
  POST /internal/recordings/speaker-timeline — active-speaker samples (at leave)

Audio chunks are streamed to MinIO via ``recording_store``; the speaker timeline
is stashed on ``meeting.data`` so the post-meeting batch job can map Deepgram
diarization indices to participant names.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from sqlalchemy.orm.attributes import flag_modified

from .models import Meeting
from .database import async_session_local
from . import recording_store
from .collector.processors import verify_meeting_token

logger = logging.getLogger("meeting_api.recordings")

router = APIRouter()


def _auth_meeting_id(authorization: Optional[str]) -> int:
    """Verify the Bearer MeetingToken and return its meeting_id claim."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    claims = verify_meeting_token(token)
    if not claims:
        raise HTTPException(status_code=401, detail="Invalid or expired meeting token")
    return int(claims["meeting_id"])


@router.post("/internal/recordings/chunk", include_in_schema=False)
async def upload_recording_chunk(
    metadata: str = Form(...),
    file: UploadFile = File(...),
    chunk_seq: Optional[str] = Form(None),
    is_final: Optional[str] = Form(None),
    authorization: Optional[str] = Header(None),
):
    """Receive one audio chunk from the bot and persist it to MinIO.

    Token's meeting_id must match the chunk metadata's meeting_id — a bot can
    only write its own meeting's audio.
    """
    token_meeting_id = _auth_meeting_id(authorization)

    try:
        meta = json.loads(metadata or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="metadata is not valid JSON")

    meeting_id = int(meta.get("meeting_id") or token_meeting_id)
    if meeting_id != token_meeting_id:
        raise HTTPException(status_code=403, detail="meeting_id does not match token")

    session_uid = meta.get("session_uid")
    if not session_uid:
        raise HTTPException(status_code=400, detail="session_uid missing from metadata")

    fmt = (meta.get("format") or "webm").lstrip(".")
    seq = int(meta.get("chunk_seq", chunk_seq if chunk_seq is not None else 0))
    final = str(meta.get("is_final", is_final)).strip().lower() in {"1", "true", "yes", "on"}

    data = await file.read()
    try:
        key = await recording_store.store_chunk(meeting_id, session_uid, seq, fmt, data)
    except Exception as e:
        logger.error(f"recordings: failed to store chunk seq={seq} meeting={meeting_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"chunk store failed: {e}")

    # On the final chunk, record where the audio lives so the post-meeting batch
    # job knows a recording exists and which session/format to assemble.
    if final:
        try:
            async with async_session_local() as db:
                meeting = await db.get(Meeting, meeting_id)
                if meeting:
                    data_obj = dict(meeting.data or {})
                    rec = dict(data_obj.get("recording") or {})
                    rec.update({
                        "session_uid": session_uid,
                        "format": fmt,
                        "chunk_count": seq + 1,
                        "complete": True,
                    })
                    data_obj["recording"] = rec
                    meeting.data = data_obj
                    flag_modified(meeting, "data")
                    await db.commit()
        except Exception as e:
            # Non-fatal: the batch job also self-discovers chunks via MinIO list.
            logger.warning(f"recordings: could not mark recording complete for meeting {meeting_id}: {e}")

    return {"ok": True, "key": key, "seq": seq, "is_final": final}


@router.post("/internal/recordings/speaker-timeline", include_in_schema=False)
async def upload_speaker_timeline(
    payload: dict,
    authorization: Optional[str] = Header(None),
):
    """Store the bot's active-speaker timeline for post-meeting speaker mapping.

    Payload: {meeting_id, session_uid, recording_start_epoch_ms,
              samples: [{t_ms, speaking: [names...]}, ...]}
    Times are relative to the recording's first-audio instant (t_ms=0), the same
    zero Deepgram uses, so diarized time ranges overlap directly with samples.
    """
    token_meeting_id = _auth_meeting_id(authorization)
    meeting_id = int(payload.get("meeting_id") or token_meeting_id)
    if meeting_id != token_meeting_id:
        raise HTTPException(status_code=403, detail="meeting_id does not match token")

    samples = payload.get("samples") or []
    async with async_session_local() as db:
        meeting = await db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="meeting not found")
        data_obj = dict(meeting.data or {})
        data_obj["speaker_timeline"] = {
            "session_uid": payload.get("session_uid"),
            "recording_start_epoch_ms": payload.get("recording_start_epoch_ms"),
            "samples": samples,
        }
        meeting.data = data_obj
        flag_modified(meeting, "data")
        await db.commit()

    logger.info(f"recordings: stored speaker timeline for meeting {meeting_id} ({len(samples)} samples)")
    return {"ok": True, "samples": len(samples)}
