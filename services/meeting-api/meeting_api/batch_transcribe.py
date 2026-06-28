"""Post-meeting batch transcription.

Replaces the realtime per-chunk transcript (which auto-detected language per
chunk and flip-flopped on Hindi/English code-switching) with a single
whole-meeting pass:

  1. Assemble every audio chunk the bot streamed to MinIO into one blob.
  2. Send it ONCE to transcription-service /v1/transcribe/batch — Deepgram nova-3
     with language='multi' + speaker diarization.
  3. Map Deepgram's anonymous speaker indices to real participant names using the
     bot's active-speaker timeline (Google's own speaking indicator — independent
     of the realtime tile/element vote-lock that mislabeled speakers).
  4. Replace the meeting's Postgres rows + Cloudflare D1 mirror with the result.

Invoked once per meeting from post_meeting.run_all_tasks(). Idempotent: a second
run is a no-op unless meeting.data['batch_transcribed'] is cleared.
"""
from __future__ import annotations

import os
import logging
from typing import Dict, List, Optional

import httpx
from sqlalchemy import text as sql_text
from sqlalchemy.orm.attributes import flag_modified

from .models import Meeting
from .database import async_session_local
from . import recording_store
from .collector.db_writer import create_transcription_object, _resolve_owner_emails
from .collector.d1_forwarder import forward_segments_to_d1, delete_meeting_segments_from_d1

logger = logging.getLogger("meeting_api.batch_transcribe")

_BATCH_LANGUAGE = os.getenv("BATCH_TRANSCRIBE_LANGUAGE", "").strip()  # "" -> service default ('multi')
_BATCH_TIMEOUT_S = float(os.getenv("BATCH_TRANSCRIBE_TIMEOUT_SECONDS", "600"))


def _service_base_url() -> str:
    url = (os.getenv("TRANSCRIPTION_SERVICE_URL", "") or "").strip().rstrip("/")
    # The env may point at the realtime endpoint; normalize to the service root.
    if url.endswith("/v1/audio/transcriptions"):
        url = url[: -len("/v1/audio/transcriptions")]
    return url


def _map_speaker_names(
    segments: List[dict], timeline_samples: List[dict]
) -> Dict[int, str]:
    """Vote each Deepgram speaker_index -> participant name via timeline overlap.

    For every diarized segment, the names marked 'speaking' in the timeline
    samples that fall within the segment's [start,end] window get a vote for that
    segment's speaker index. The winning name per index is its mapping.
    """
    if not timeline_samples:
        return {}

    # Pre-sort samples by time for a simple linear scan per segment.
    samples = sorted(
        (s for s in timeline_samples if s.get("t_ms") is not None),
        key=lambda s: s["t_ms"],
    )
    votes: Dict[int, Dict[str, float]] = {}
    for seg in segments:
        idx = seg.get("speaker_index")
        if idx is None:
            continue
        start_ms = float(seg.get("start", 0.0)) * 1000.0
        end_ms = float(seg.get("end", 0.0)) * 1000.0
        bucket = votes.setdefault(int(idx), {})
        for s in samples:
            t = s["t_ms"]
            if t < start_ms:
                continue
            if t > end_ms:
                break
            speaking = s.get("speaking") or []
            # When exactly one tile is speaking the attribution is unambiguous;
            # split the vote when several are lit so a clear single speaker wins.
            weight = 1.0 / len(speaking) if speaking else 0.0
            for name in speaking:
                if name:
                    bucket[name] = bucket.get(name, 0.0) + weight

    mapping: Dict[int, str] = {}
    for idx, bucket in votes.items():
        if bucket:
            mapping[idx] = max(bucket, key=bucket.get)
    return mapping


async def _call_batch_service(audio_bytes: bytes, fmt: str) -> Optional[dict]:
    base = _service_base_url()
    if not base:
        logger.error("batch_transcribe: TRANSCRIPTION_SERVICE_URL not set; cannot transcribe")
        return None
    url = f"{base}/v1/transcribe/batch"
    token = (os.getenv("TRANSCRIPTION_SERVICE_TOKEN", "") or "").strip()
    headers = {"X-API-Key": token} if token else {}
    data = {"diarize": "true"}
    if _BATCH_LANGUAGE:
        data["language"] = _BATCH_LANGUAGE
    files = {"file": (f"meeting.{fmt}", audio_bytes, f"audio/{fmt}")}

    timeout = httpx.Timeout(_BATCH_TIMEOUT_S, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, data=data, files=files)
    if resp.status_code != 200:
        logger.error(f"batch_transcribe: service returned {resp.status_code}: {resp.text[:500]}")
        return None
    return resp.json()


async def batch_transcribe_meeting(meeting_id: int, force: bool = False) -> bool:
    """Run the whole-meeting batch transcript. Returns True if it wrote segments."""
    # 1. Load meeting + recording metadata; bail early if nothing to do.
    async with async_session_local() as db:
        meeting = await db.get(Meeting, meeting_id)
        if not meeting:
            logger.error(f"batch_transcribe: meeting {meeting_id} not found")
            return False
        data_obj = dict(meeting.data or {})
        if data_obj.get("batch_transcribed") and not force:
            logger.info(f"batch_transcribe: meeting {meeting_id} already batch-transcribed; skipping")
            return False
        rec = data_obj.get("recording") or {}
        session_uid = rec.get("session_uid")
        timeline = (data_obj.get("speaker_timeline") or {}).get("samples") or []

    # 2. Assemble the full audio from MinIO chunks.
    try:
        audio_bytes, fmt, n_chunks = await recording_store.assemble_meeting_audio(meeting_id, session_uid)
    except Exception as e:
        logger.error(f"batch_transcribe: failed to assemble audio for meeting {meeting_id}: {e}", exc_info=True)
        return False
    if not audio_bytes:
        logger.info(f"batch_transcribe: no recorded audio for meeting {meeting_id}; nothing to transcribe")
        return False
    logger.info(
        f"batch_transcribe: meeting {meeting_id} assembled {len(audio_bytes)} bytes "
        f"from {n_chunks} {fmt} chunk(s); sending to Deepgram batch"
    )

    # 3. Whole-file diarized transcription.
    result = await _call_batch_service(audio_bytes, fmt or "webm")
    if not result:
        return False
    segments = result.get("segments") or []
    if not segments:
        logger.info(f"batch_transcribe: meeting {meeting_id} produced 0 segments")
        return False

    # 4. Map diarization indices -> names via the active-speaker timeline.
    speaker_map = _map_speaker_names(segments, timeline)
    if speaker_map:
        logger.info(f"batch_transcribe: meeting {meeting_id} speaker map: {speaker_map}")
    else:
        logger.warning(f"batch_transcribe: meeting {meeting_id} no speaker timeline; using generic labels")

    # 5. Build Transcription rows.
    rows = []
    for idx, seg in enumerate(segments):
        spk_idx = seg.get("speaker_index")
        if spk_idx is not None and spk_idx in speaker_map:
            speaker_name = speaker_map[spk_idx]
        elif spk_idx is not None:
            speaker_name = f"Speaker {int(spk_idx) + 1}"
        else:
            speaker_name = None
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        rows.append(create_transcription_object(
            meeting_id=meeting_id,
            start=float(seg.get("start", 0.0)),
            end=float(seg.get("end", 0.0)),
            text=text,
            language=seg.get("language"),
            session_uid=session_uid,
            mapped_speaker_name=speaker_name,
            segment_id=f"batch-{idx:05d}",
        ))
    if not rows:
        logger.info(f"batch_transcribe: meeting {meeting_id} had only empty segments")
        return False

    # 6. Replace Postgres rows for this meeting with the batch transcript.
    async with async_session_local() as db:
        try:
            await db.execute(
                sql_text("DELETE FROM transcriptions WHERE meeting_id = :mid"),
                {"mid": meeting_id},
            )
            for t in rows:
                db.add(t)
            owner_emails = await _resolve_owner_emails(db, {meeting_id})
            # Mark complete on the same commit so a crash mid-mirror doesn't redo
            # the (idempotent) Postgres write but still allows D1 retry via force.
            meeting = await db.get(Meeting, meeting_id)
            if meeting:
                d = dict(meeting.data or {})
                d["batch_transcribed"] = True
                d["batch_segment_count"] = len(rows)
                meeting.data = d
                flag_modified(meeting, "data")
            await db.commit()
            logger.info(f"batch_transcribe: meeting {meeting_id} wrote {len(rows)} segments to Postgres")

            # 7. Mirror to Cloudflare D1 (replace stale realtime rows first).
            # Done INSIDE the session: after commit the ORM rows are expired, so
            # forward_segments_to_d1 (which reads row attributes) must run while
            # the session is still open or it reads detached instances and the
            # mirror silently no-ops. Best-effort — never fails the Postgres write.
            try:
                await delete_meeting_segments_from_d1(meeting_id)
                await forward_segments_to_d1(rows, owner_emails)
            except Exception as e:
                logger.error(f"batch_transcribe: D1 mirror failed for meeting {meeting_id} (non-fatal): {e}", exc_info=True)
        except Exception as e:
            await db.rollback()
            logger.error(f"batch_transcribe: Postgres write failed for meeting {meeting_id}: {e}", exc_info=True)
            return False

    return True
