"""Post-meeting batch transcription.

Replaces the realtime per-chunk transcript (which auto-detected language per
chunk and flip-flopped on Hindi/English code-switching) with a single
whole-meeting pass:

  1. Assemble every audio chunk the bot streamed to MinIO into one blob.
  2. Send it ONCE to transcription-service /v1/transcribe/batch — Deepgram nova-3
     with language='multi' + speaker diarization.
  3. Name each segment from the bot's active-speaker timeline (Google's own
     speaking indicator), per segment rather than per Deepgram speaker index —
     see _attribute_speakers for why the per-index vote collapsed multi-speaker
     meetings onto whoever talked most.
  4. Replace the meeting's Postgres rows + Cloudflare D1 mirror with the result.

Invoked once per meeting from post_meeting.run_all_tasks(). Idempotent: a second
run is a no-op unless meeting.data['batch_transcribed'] is cleared.
"""
from __future__ import annotations

import bisect
import os
import logging
import time
from typing import Dict, List, Optional, Tuple

import httpx
from sqlalchemy import select, text as sql_text
from sqlalchemy.orm.attributes import flag_modified

from .models import Meeting
from .database import async_session_local
from . import recording_store
from .collector.db_writer import create_transcription_object, _resolve_owner_emails
from .collector.d1_forwarder import forward_segments_to_d1, delete_meeting_segments_from_d1

logger = logging.getLogger("meeting_api.batch_transcribe")

_BATCH_LANGUAGE = os.getenv("BATCH_TRANSCRIBE_LANGUAGE", "").strip()  # "" -> service default ('multi')
_BATCH_TIMEOUT_S = float(os.getenv("BATCH_TRANSCRIBE_TIMEOUT_SECONDS", "600"))
# How long a claim is honoured before a later caller may treat the run as dead
# and retry it. Must exceed the worst-case run (audio assembly + the Deepgram
# call, itself capped at _BATCH_TIMEOUT_S) or a slow-but-healthy run would be
# duplicated by the very mechanism meant to prevent duplicates.
_CLAIM_TTL_S = float(os.getenv("BATCH_TRANSCRIBE_CLAIM_TTL_SECONDS", "1800"))


def _service_base_url() -> str:
    url = (os.getenv("TRANSCRIPTION_SERVICE_URL", "") or "").strip().rstrip("/")
    # The env may point at the realtime endpoint; normalize to the service root.
    if url.endswith("/v1/audio/transcriptions"):
        url = url[: -len("/v1/audio/transcriptions")]
    return url


# Progressively widen the window when a segment contains no timeline sample.
# Samples land every ~500ms, so a short utterance can genuinely straddle two of
# them; a few seconds of slack recovers it without reaching so far that we
# borrow a neighbouring speaker's turn.
_TIMELINE_PAD_MS = (0, 500, 1500, 3000)


def _votes_in_window(
    samples: List[dict], ts: List[float], start_ms: float, end_ms: float, pad_ms: float
) -> Dict[str, float]:
    """Weighted vote of the names lit in [start-pad, end+pad]."""
    bucket: Dict[str, float] = {}
    i = bisect.bisect_left(ts, start_ms - pad_ms)
    limit = end_ms + pad_ms
    while i < len(ts) and ts[i] <= limit:
        speaking = samples[i].get("speaking") or []
        # When exactly one tile is lit the attribution is unambiguous; split the
        # vote when several are lit so a clear single speaker still wins.
        if speaking:
            weight = 1.0 / len(speaking)
            for name in speaking:
                if name:
                    bucket[name] = bucket.get(name, 0.0) + weight
        i += 1
    return bucket


def _attribute_speakers(
    segments: List[dict], timeline_samples: List[dict]
) -> Tuple[List[Optional[str]], Dict[int, str]]:
    """Resolve a participant name for each diarized segment.

    Attribution is PER SEGMENT against the active-speaker timeline, not per
    Deepgram speaker_index; the index is only a fallback for segments the
    timeline does not cover.

    Why: the timeline is ground truth — Google Meet tells us exactly whose tile
    is lit — whereas Deepgram's diarization of a single mixed-down, multilingual
    stream does not cleanly separate voices. Voting per index and taking an
    independent argmax per index (the previous approach) let the meeting's
    dominant speaker win nearly every index: in meeting 66 one participant was
    lit 78% of the time and captured 5 of Deepgram's 6 indices, so ~184 segments
    actually spoken by three other people were labelled with his name and two
    participants disappeared from the transcript entirely.

    Returns (per-segment names, index->name map). The map is kept only for the
    fallback path and for logging.
    """
    if not timeline_samples:
        return [None] * len(segments), {}

    samples = sorted(
        (s for s in timeline_samples if s.get("t_ms") is not None),
        key=lambda s: s["t_ms"],
    )
    ts = [s["t_ms"] for s in samples]

    names: List[Optional[str]] = []
    index_votes: Dict[int, Dict[str, float]] = {}

    for seg in segments:
        start_ms = float(seg.get("start", 0.0)) * 1000.0
        end_ms = float(seg.get("end", 0.0)) * 1000.0

        bucket: Dict[str, float] = {}
        for pad in _TIMELINE_PAD_MS:
            bucket = _votes_in_window(samples, ts, start_ms, end_ms, pad)
            if bucket:
                break

        names.append(max(bucket, key=bucket.get) if bucket else None)

        # Tally the index only from segments the timeline actually covered, so
        # the fallback below isn't polluted by guesses.
        idx = seg.get("speaker_index")
        if idx is not None and bucket:
            tally = index_votes.setdefault(int(idx), {})
            for name, w in bucket.items():
                tally[name] = tally.get(name, 0.0) + w

    index_map: Dict[int, str] = {
        idx: max(tally, key=tally.get) for idx, tally in index_votes.items() if tally
    }

    # Fallback: a segment with no timeline coverage inherits its index's name.
    for i, seg in enumerate(segments):
        if names[i] is None:
            idx = seg.get("speaker_index")
            if idx is not None:
                names[i] = index_map.get(int(idx))

    return names, index_map


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


async def _claim_batch_run(meeting_id: int, force: bool) -> Optional[dict]:
    """Atomically take ownership of this meeting's batch run.

    Returns the meeting's data dict if we own the run, else None.

    run_all_tasks() fires from BOTH the container-exit callback and the terminal
    status_change handler (callbacks.py), so two invocations routinely race. A
    plain read of ``batch_transcribed`` cannot stop them: the flag is only set
    ~30s later, after the audio is assembled and Deepgram has answered, so both
    callers read False and both bill a full transcription of the same audio.
    Meeting 66 was transcribed twice, one second apart.

    Claiming under a row lock (same pattern as outbound_events.claim_outbound_event)
    closes the window: exactly one caller writes the claim and proceeds. A claim
    older than _CLAIM_TTL_S is treated as abandoned so a crashed run can be retried.
    """
    async with async_session_local() as db:
        try:
            meeting = (
                await db.execute(
                    select(Meeting)
                    .where(Meeting.id == meeting_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if not meeting:
                logger.error(f"batch_transcribe: meeting {meeting_id} not found")
                await db.rollback()
                return None

            data_obj = dict(meeting.data or {})

            if data_obj.get("batch_transcribed") and not force:
                logger.info(f"batch_transcribe: meeting {meeting_id} already batch-transcribed; skipping")
                await db.rollback()
                return None

            claimed_at = data_obj.get("batch_transcribe_claimed_at")
            if claimed_at is not None and not force:
                age = time.time() - float(claimed_at)
                if age < _CLAIM_TTL_S:
                    logger.info(
                        f"batch_transcribe: meeting {meeting_id} already claimed {age:.0f}s ago "
                        f"by a concurrent run; skipping (would have double-billed Deepgram)"
                    )
                    await db.rollback()
                    return None
                logger.warning(
                    f"batch_transcribe: meeting {meeting_id} reclaiming stale run "
                    f"({age:.0f}s old, TTL {_CLAIM_TTL_S}s) — previous attempt likely crashed"
                )

            data_obj["batch_transcribe_claimed_at"] = time.time()
            meeting.data = data_obj
            flag_modified(meeting, "data")
            await db.commit()
            return data_obj
        except Exception as e:
            await db.rollback()
            logger.error(f"batch_transcribe: failed to claim meeting {meeting_id}: {e}", exc_info=True)
            return None


async def _release_claim(meeting_id: int) -> None:
    """Drop the claim after a failed run so the meeting can be retried."""
    try:
        async with async_session_local() as db:
            meeting = await db.get(Meeting, meeting_id)
            if meeting:
                d = dict(meeting.data or {})
                if d.pop("batch_transcribe_claimed_at", None) is not None:
                    meeting.data = d
                    flag_modified(meeting, "data")
                    await db.commit()
    except Exception as e:
        logger.error(f"batch_transcribe: failed to release claim on meeting {meeting_id}: {e}")


async def batch_transcribe_meeting(meeting_id: int, force: bool = False) -> bool:
    """Run the whole-meeting batch transcript. Returns True if it wrote segments."""
    # 1. Take the run under a row lock, so concurrent callers can't both pay
    #    Deepgram for the same audio. Everything below is guarded by the claim.
    data_obj = await _claim_batch_run(meeting_id, force)
    if data_obj is None:
        return False

    ok = False
    try:
        ok = await _run_claimed_batch(meeting_id, data_obj)
        return ok
    finally:
        # A run that didn't finish leaves no batch_transcribed flag, so drop the
        # claim to let a later attempt (sweep / manual force) retry it. A success
        # keeps the claim alongside the flag — harmless, and it records the run.
        if not ok:
            await _release_claim(meeting_id)


async def _run_claimed_batch(meeting_id: int, data_obj: dict) -> bool:
    """The batch run proper. Only ever called by the caller holding the claim."""
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

    # 4. Attribute a name to each segment via the active-speaker timeline.
    seg_names, speaker_map = _attribute_speakers(segments, timeline)
    if speaker_map:
        attributed = sum(1 for n in seg_names if n)
        logger.info(
            f"batch_transcribe: meeting {meeting_id} attributed {attributed}/{len(segments)} "
            f"segments to {len(set(n for n in seg_names if n))} speaker(s); "
            f"index fallback map: {speaker_map}"
        )
    else:
        logger.warning(f"batch_transcribe: meeting {meeting_id} no speaker timeline; using generic labels")

    # 5. Build Transcription rows.
    rows = []
    for idx, seg in enumerate(segments):
        spk_idx = seg.get("speaker_index")
        speaker_name = seg_names[idx]
        if speaker_name is None and spk_idx is not None:
            speaker_name = f"Speaker {int(spk_idx) + 1}"
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
