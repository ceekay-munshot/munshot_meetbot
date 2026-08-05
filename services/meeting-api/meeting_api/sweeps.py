"""v0.10.5 Pack E.3.2 + Pack D.2 + H.4 + E.1-sibling sweeps.

Long-running idle-loop equivalent for meeting-api. Each sweep is a
periodic scan that catches state-machine rows that genuinely got
stuck — escapes from the canonical durable mechanisms (Pack J's
exit-callback in callbacks.py, etc).

Active responsibilities:
  - Pack E.3.2: stale-stopping sweep (postgres scan + force-finalize).
  - Pack H.4: aggregation retry for transient infra failures.
  - Pack D.2 (#266): durable container-stop outbox consumer
    (Redis Stream `meeting-api:container-stops` → runtime-api DELETE
    with retry + DLQ). The producer side is `_delayed_container_stop`
    in meetings.py, which now XADDs onto the stream instead of running
    an in-process timer.

Principle filter: every sweep is OBSERVABLE. Rows found = the canonical
mechanism failed somewhere; operators must see it. Loud warning logs
on each row + a per-iteration summary count. Pack M wires Prometheus
counter increments here when metrics infra ships.

Pattern mirrors webhook_retry_worker.py — same shape, different
responsibility. Spawned from main.py startup alongside the retry worker.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from .models import Meeting
from .schemas import MeetingStatus, MeetingCompletionReason

logger = logging.getLogger("meeting_api.sweeps")

# v0.10.5 Pack E.3.2 — stale-stopping sweep config.
#
# Threshold is generous: the runtime-api exit callback (260421 Pack J)
# is the canonical durable mechanism for stopping → completed; legitimate
# stops complete in well under 90 s. A row stuck in 'stopping' for 5+ min
# means the canonical path genuinely failed — log loud, force-finalize.
STALE_STOPPING_THRESHOLD_SECONDS = 300  # 5 min
STALE_STOPPING_POLL_INTERVAL = 60  # check every 60 s

# v0.10.5 Pack K.5 (meeting-api side analog).
# Module-level state for /health probe / Pack M metrics.
sweep_iterations: int = 0
sweep_last_iteration_at: float = 0.0

_stop_event: Optional[asyncio.Event] = None


async def _sweep_stale_stopping(
    db_session_factory: Callable[[], AsyncSession],
) -> int:
    """One iteration of the stale-stopping sweep.

    Scans for rows where status='stopping' AND the time since the meeting
    last *progressed* (last status_transition.timestamp/at, fall back to
    created_at) exceeds STALE_STOPPING_THRESHOLD_SECONDS. Force-completes
    each with
    `completion_reason=STOPPED` + transition_reason='stale_stopping_sweep'
    so the source is visible in audit logs.

    #313 — pre-fix used `updated_at` as the staleness predicate, which is
    bumped by every webhook-retry. A meeting stuck in 'stopping' with
    active webhook retries kept looking fresh and the sweep never fired.
    Now we read the immutable transition timestamps from
    `data.status_transition` (append-only history), which reflects actual
    progress; webhook retries do not append to that list.

    Returns the number of rows swept. Operators reading logs see:
      WARNING [sweep] meeting <id> stuck stopping for X s — finalizing
    Each row found indicates the canonical exit-callback path failed.

    Idempotent: force-completing an already-completed meeting is a no-op
    (status is already terminal).
    """
    from datetime import datetime, timedelta
    from .meetings import update_meeting_status, publish_meeting_status_change, get_redis

    threshold = datetime.utcnow() - timedelta(seconds=STALE_STOPPING_THRESHOLD_SECONDS)
    swept = 0

    async with db_session_factory() as db:
        # SQL pre-filter: status='stopping' AND created_at < threshold.
        # created_at is immutable so it's safe; updated_at is poisoned by
        # webhook retries (#313) and other JSONB writes, so it cannot prove
        # lifecycle progress. Post-filter by status_transition below.
        stmt = (
            select(Meeting)
            .where(Meeting.status == MeetingStatus.STOPPING.value)
            .where(Meeting.created_at < threshold)
            .limit(200)  # candidate cap — we post-filter in Python
        )
        candidates = (await db.execute(stmt)).scalars().all()

        # Post-filter: compute the actual last-progress timestamp from
        # data.status_transition (append-only). Falls back to created_at
        # for rows missing the JSONB history (legacy data).
        rows = []
        for meeting in candidates:
            data = (meeting.data or {}) if isinstance(meeting.data, dict) else {}
            transitions = data.get("status_transition") or []
            last_progress_at = meeting.created_at
            for transition in transitions:
                if not isinstance(transition, dict):
                    continue
                # Pack 4 writes 'timestamp'; pre-pack-4 history wrote 'at'.
                at_str = transition.get("timestamp") or transition.get("at")
                if not at_str:
                    continue
                try:
                    at_dt = datetime.fromisoformat(at_str.replace("Z", "+00:00"))
                    # Strip tzinfo to compare with naive utcnow()-derived threshold.
                    if at_dt.tzinfo is not None:
                        at_dt = at_dt.replace(tzinfo=None)
                except (TypeError, ValueError, AttributeError):
                    continue
                if at_dt > last_progress_at:
                    last_progress_at = at_dt
            if last_progress_at < threshold:
                rows.append((meeting, last_progress_at))
            if len(rows) >= 50:  # bound work per iteration
                break

        for meeting, last_progress_at in rows:
            stuck_for = (datetime.utcnow() - last_progress_at).total_seconds()
            logger.warning(
                f"[sweep] meeting {meeting.id} stuck stopping for {stuck_for:.0f}s — "
                f"finalizing via stale-stopping sweep "
                f"(canonical exit-callback path appears to have failed)"
            )
            try:
                # Use Pack J's classifier to route correctly — even though
                # we're forcing the finalize, the classifier's principle
                # (positive proof of success vs default-to-failed) still
                # applies. If the meeting genuinely had no segments, this
                # routes to STOPPED_WITH_NO_AUDIO; if it ran clean, STOPPED.
                from .callbacks import _classify_stopped_exit
                target_status, classified_reason = await _classify_stopped_exit(
                    meeting, db, MeetingCompletionReason.STOPPED
                )
                success = await update_meeting_status(
                    meeting,
                    target_status,
                    db,
                    completion_reason=classified_reason,
                    transition_reason="stale_stopping_sweep",
                    transition_metadata={
                        "sweep_source": "Pack E.3.2",
                        "stuck_for_seconds": int(stuck_for),
                        "pack_j_classification": classified_reason.value,
                    },
                )
                if success:
                    swept += 1
                    # Notify dashboard via WS pubsub
                    redis_client = get_redis()
                    if redis_client:
                        await publish_meeting_status_change(
                            meeting.id,
                            target_status.value,
                            redis_client,
                            meeting.platform,
                            meeting.platform_specific_id,
                            meeting.user_id,
                        )
            except Exception as e:
                logger.error(
                    f"[sweep] failed to finalize stuck meeting {meeting.id}: {e}",
                    exc_info=True,
                )

    return swept


async def _sweep_aggregation_retry(
    db_session_factory: Callable[[], AsyncSession],
) -> int:
    """v0.10.5 Pack H.4 — retry meetings stuck on transient-infra aggregation failure.

    Scans `data->>'aggregation_failure_class' = 'transient_infra'` AND
    `data->>'aggregation_last_retry_at'` older than the next-attempt
    backoff window. For each, re-attempts aggregate_transcription. On
    success: clears failure_class. On 24-attempt budget exhaustion
    (~7 days at exponential backoff): flips to 'permanent_infra' +
    fires critical alert (Pack M wires the actual Prometheus counter
    when metrics infra ships).

    Returns count of rows successfully retried this iteration.
    """
    from datetime import datetime, timedelta
    from .models import Meeting

    BUDGET_ATTEMPTS = 24  # 7 days at exponential backoff
    swept = 0

    # Backoff schedule: 1m, 5m, 15m, 30m, 1h, 2h, 4h, 8h, 16h, 24h × N
    # Keep simple — use retry_count to determine next-eligible time.
    def _eligible_for_retry(retry_count: int, last_retry_at_str: str) -> bool:
        try:
            last_retry = datetime.fromisoformat(last_retry_at_str)
        except (ValueError, TypeError):
            return True
        # Backoff: 60s base, 2× per attempt, capped at 24h
        backoff_s = min(60 * (2 ** min(retry_count, 10)), 86400)
        return datetime.utcnow() - last_retry > timedelta(seconds=backoff_s)

    async with db_session_factory() as db:
        from sqlalchemy import text
        # Use JSONB query — meetings.data->>'aggregation_failure_class' = 'transient_infra'
        stmt = text("""
            SELECT id FROM meetings
            WHERE data->>'aggregation_failure_class' = :cls
            ORDER BY (data->>'aggregation_last_retry_at')::timestamp NULLS FIRST
            LIMIT 50
        """)
        rows = (await db.execute(stmt, {"cls": "transient_infra"})).fetchall()

        if not rows:
            return 0

        from .post_meeting import (
            aggregate_transcription,
            set_aggregation_failure_class,
            AggregationFailureClass,
        )

        for row in rows:
            meeting_id = row[0]
            meeting = await db.get(Meeting, meeting_id)
            if not meeting:
                continue
            data = meeting.data or {}
            retry_count = data.get("aggregation_retry_count") or 0
            last_retry = data.get("aggregation_last_retry_at") or ""

            # Budget exhausted — flip to permanent + emit critical event
            if retry_count >= BUDGET_ATTEMPTS:
                logger.error(
                    f"[sweep] Pack H.4: meeting {meeting_id} exhausted aggregation "
                    f"retry budget after {retry_count} attempts — flipping to "
                    f"'permanent_infra' + critical alert"
                )
                set_aggregation_failure_class(
                    meeting, AggregationFailureClass.PERMANENT_INFRA
                )
                await db.commit()
                # TODO: emit meeting.aggregation_failed_permanent webhook event
                # (Pack H.3 wire-up — webhook_delivery infrastructure exists;
                # event dispatch lands in next commit)
                continue

            # Within budget — check eligibility
            if not _eligible_for_retry(retry_count, last_retry):
                continue

            try:
                ok = await aggregate_transcription(meeting, db)
                if ok:
                    logger.info(
                        f"[sweep] Pack H.4: meeting {meeting_id} aggregation "
                        f"retry {retry_count + 1} succeeded"
                    )
                    swept += 1
                else:
                    # Still transient — set_aggregation_failure_class inside
                    # aggregate_transcription already incremented retry_count.
                    logger.debug(
                        f"[sweep] Pack H.4: meeting {meeting_id} aggregation "
                        f"retry {retry_count + 1} still transient"
                    )
            except Exception as e:
                logger.error(
                    f"[sweep] Pack H.4 aggregation retry failed for {meeting_id}: "
                    f"{type(e).__name__}: {e!r}",
                    exc_info=True,
                )

    return swept


# Audio-retention sweep config. Raw MinIO recording chunks are only needed
# long enough for batch_transcribe.py to assemble + transcribe them once;
# after that they just cost storage, so purge them AUDIO_RETENTION_DAYS after
# the meeting was created. Checked on its own (slow) cadence — the query is
# cheap when there's nothing to do, but there's no reason to hit MinIO's
# list/delete API every 60s like the other sweeps.
AUDIO_RETENTION_DAYS = float(os.getenv("AUDIO_RETENTION_DAYS", "7"))
AUDIO_RETENTION_POLL_INTERVAL = 3600  # check hourly

_last_audio_retention_sweep_at: float = 0.0


async def _sweep_audio_retention(
    db_session_factory: Callable[[], AsyncSession],
) -> int:
    """Purge stored media for finalized meetings older than AUDIO_RETENTION_DAYS.

    Removes the meeting's MinIO audio chunks and its failure-diagnostic
    screenshots, and drops the active-speaker timeline from `data` (it only
    describes audio that no longer exists, and it is by far the largest JSONB
    key — ~2 samples/second for the whole meeting).

    Marks one of two keys so a later iteration doesn't re-scan the meeting:
      - `data.recording.audio_deleted_at`         — media was actually deleted
      - `data.recording.audio_retention_checked_at` — nothing was stored
    They stay distinct because /audio reports "expired per retention policy" off
    the former; marking it on a meeting that never recorded would lie.

    The candidate query deliberately does NOT require `data->'recording'` to
    exist. It used to, and that made this sweep dead code for its entire life:
    `data.recording` is only written when a chunk arrives with is_final=true, and
    the bot dropped that chunk whenever it was zero-length (fixed in
    vexa-bot audio-pipeline `_handleChunk`), so the key was absent on 100% of
    meetings and this WHERE clause matched 0 rows while audio accumulated
    indefinitely. The retention guarantee must not depend on a bot-side success
    signal — a meeting whose bot crashed is exactly the one whose audio needs
    collecting. `->>` on a missing key yields NULL, so absent-key rows qualify.
    """
    from . import recording_store

    threshold = datetime.utcnow() - timedelta(days=AUDIO_RETENTION_DAYS)
    swept = 0      # meetings whose media we actually deleted
    checked = 0    # meetings marked as having nothing stored

    async with db_session_factory() as db:
        stmt = text("""
            SELECT id FROM meetings
            WHERE status IN (:completed, :failed)
              AND created_at < :threshold
              AND data->'recording'->>'audio_deleted_at' IS NULL
              AND data->'recording'->>'audio_retention_checked_at' IS NULL
            ORDER BY created_at
            LIMIT 200
        """)
        rows = (await db.execute(stmt, {
            "completed": MeetingStatus.COMPLETED.value,
            "failed": MeetingStatus.FAILED.value,
            "threshold": threshold,
        })).fetchall()

        for row in rows:
            meeting_id = row[0]
            try:
                deleted = await recording_store.delete_meeting_audio(meeting_id)
                shots = await recording_store.delete_meeting_screenshots(meeting_id)
            except Exception as e:
                logger.error(
                    f"[sweep] audio-retention: failed to delete stored media for meeting {meeting_id}: {e}",
                    exc_info=True,
                )
                continue

            meeting = await db.get(Meeting, meeting_id)
            if not meeting:
                continue
            data_obj = dict(meeting.data or {})
            rec = dict(data_obj.get("recording") or {})
            now_iso = datetime.utcnow().isoformat() + "Z"
            if deleted:
                rec["audio_deleted_at"] = now_iso
                rec["audio_chunks_deleted"] = deleted
                # The timeline maps diarized segments onto audio we just deleted;
                # keep the count for forensics, drop the samples.
                timeline = data_obj.get("speaker_timeline")
                if isinstance(timeline, dict) and timeline.get("samples"):
                    timeline = dict(timeline)
                    timeline["samples_pruned"] = len(timeline["samples"])
                    timeline["samples"] = []
                    timeline["pruned_at"] = now_iso
                    data_obj["speaker_timeline"] = timeline
            else:
                rec["audio_retention_checked_at"] = now_iso
            if shots:
                rec["screenshots_deleted"] = shots
            data_obj["recording"] = rec
            meeting.data = data_obj
            flag_modified(meeting, "data")
            await db.commit()
            if deleted or shots:
                swept += 1
                logger.info(
                    f"[sweep] audio-retention: purged {deleted} chunk(s) + {shots} screenshot(s) "
                    f"for meeting {meeting_id} (older than {AUDIO_RETENTION_DAYS}d)"
                )
            else:
                checked += 1

    if checked:
        logger.debug(
            f"[sweep] audio-retention: {checked} meeting(s) past retention had no stored media"
        )
    return swept


async def _sweep_container_stops() -> dict:
    """v0.10.5 Pack D.2 (#266) — durable container-stop outbox consumer.

    One iteration of the consumer for the
    `meeting-api:container-stops` Redis Stream. Producer side is
    `_delayed_container_stop` in meetings.py. The consumer reads all
    entries due-now (fire_at <= now), invokes `_stop_via_runtime_api`
    (idempotent — runtime-api 200 no-op for already-stopped), and
    handles retry / DLQ on failure.

    Returns the consumer's per-iteration summary dict (succeeded /
    retried / dlq / deferred), or {} on Redis unavailability.

    Why here, not in a dedicated worker: the per-iteration sweep cadence
    (60 s) is sufficient for the BOT_STOP_DELAY_SECONDS=90 window, and
    co-locating with the other sweeps keeps the operational surface
    small (one supervisor, one task). Same shape as Pack H.4's
    aggregation-retry sweep above.
    """
    from .meetings import get_redis, _stop_via_runtime_api
    from .container_stop_outbox import consume_pending_stops

    redis_client = get_redis()
    if redis_client is None:
        return {}

    return await consume_pending_stops(redis_client, _stop_via_runtime_api)


async def start_sweeps(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """Run sweeps in a periodic loop. Call via asyncio.create_task().

    Currently runs:
      - Pack E.3.2: stale-stopping sweep
      - Pack H.4: aggregation_failure_class='transient_infra' retry
      - Pack D.2: container-stop outbox consumer (durable retry + DLQ)

    Pattern mirrors webhook_retry_worker.start_retry_worker — same
    shape, different responsibility.
    """
    global _stop_event, sweep_iterations, sweep_last_iteration_at, _last_audio_retention_sweep_at
    _stop_event = asyncio.Event()

    logger.info("[sweeps] Starting meeting-api idle sweeps loop (Pack E.3.2 + H.4 + D.2 + audio-retention)")

    while not _stop_event.is_set():
        sweep_iterations += 1
        sweep_last_iteration_at = time.time()

        try:
            swept = await _sweep_stale_stopping(db_session_factory)
            if swept > 0:
                logger.warning(
                    f"[sweeps] iteration {sweep_iterations}: "
                    f"swept {swept} stale-stopping rows "
                    f"(operators should investigate why exit-callback path failed)"
                )
        except Exception as e:
            logger.error(f"[sweeps] iteration {sweep_iterations} stale-stopping error: {e}", exc_info=True)

        try:
            retried = await _sweep_aggregation_retry(db_session_factory)
            if retried > 0:
                logger.info(
                    f"[sweeps] iteration {sweep_iterations}: "
                    f"successfully retried {retried} aggregation_failed rows (Pack H.4)"
                )
        except Exception as e:
            logger.error(f"[sweeps] iteration {sweep_iterations} aggregation-retry error: {e}", exc_info=True)

        if time.time() - _last_audio_retention_sweep_at >= AUDIO_RETENTION_POLL_INTERVAL:
            _last_audio_retention_sweep_at = time.time()
            try:
                purged = await _sweep_audio_retention(db_session_factory)
                if purged > 0:
                    logger.info(
                        f"[sweeps] iteration {sweep_iterations}: "
                        f"purged audio for {purged} meeting(s) past the {AUDIO_RETENTION_DAYS}d retention window"
                    )
            except Exception as e:
                logger.error(f"[sweeps] iteration {sweep_iterations} audio-retention error: {e}", exc_info=True)

        try:
            stop_summary = await _sweep_container_stops()
            if stop_summary and (
                stop_summary.get("processed") or stop_summary.get("dlq")
            ):
                logger.info(
                    f"[sweeps] iteration {sweep_iterations} container-stops (Pack D.2): {stop_summary}"
                )
                if stop_summary.get("dlq", 0) > 0:
                    logger.warning(
                        f"[sweeps] iteration {sweep_iterations}: "
                        f"{stop_summary['dlq']} container-stop entries moved to DLQ "
                        f"(meeting-api:container-stop-dlq) — operator must investigate "
                        f"persistent runtime-api communication failures"
                    )
        except Exception as e:
            logger.error(
                f"[sweeps] iteration {sweep_iterations} container-stops error: {e}",
                exc_info=True,
            )

        # Wait for POLL_INTERVAL or until stopped.
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=STALE_STOPPING_POLL_INTERVAL)
            break  # stop_event was set
        except asyncio.TimeoutError:
            pass  # normal — poll again

    logger.info(f"[sweeps] Stopped after {sweep_iterations} iterations")


async def stop_sweeps() -> None:
    """Signal the sweep loop to stop."""
    global _stop_event
    if _stop_event is not None:
        _stop_event.set()
