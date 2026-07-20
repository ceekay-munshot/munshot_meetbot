"""Scheduler loop — dispatches due rows from the Cloudflare-D1-backed
`schedules` table (owned/auto-created by the Cloudflare Worker's dashboard
side; this service only reads/updates rows, never the schema).

Same shape as `sync_loop()` in main.py: a single sequential
`while True: do_tick(); await asyncio.sleep(interval)` background task,
started once at startup. In a single, `--workers`-less uvicorn process (true
of every service in this repo) that shape can't overlap with itself, so no
D1-side row lock/lease is needed for the current deployment topology. If this
service ever runs multiple replicas/workers, revisit with a D1 conditional
`UPDATE ... WHERE id=? AND next_run<=?` claim.

Residual risk (accepted): if POST /public/join succeeds but the process
crashes before the following D1 status write lands, the row is still "due"
and gets redispatched next tick. Mitigated by (a) retrying the D1 write
itself a few times (see d1_schedule_client._write_with_retry) and (b)
treating HTTP 409 from /public/join as success — meeting-api already dedups
bot joins by native_meeting_id, so a rare redispatch is a no-op, not a
duplicate bot.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

from meeting_api.collector.d1_schedule_client import (
    select_due_schedules,
    update_recurring_row,
    update_failed_attempt,
    delete_schedule_row,
)

from .recurrence import next_recurring_run

logger = logging.getLogger("calendar-service.scheduler")

SCHEDULE_POLL_INTERVAL_SECONDS = int(os.getenv("SCHEDULE_POLL_INTERVAL_SECONDS", "60"))
# Where the real bot-launch endpoint lives (services/api-gateway/main.py's
# POST /public/join) — not calendar-service's own same-path OAuth handler.
SCHEDULER_API_BASE_URL = os.getenv(
    "SCHEDULER_API_BASE_URL", "http://65.1.101.15.nip.io:8080"
).rstrip("/")
SCHEDULER_MAX_ATTEMPTS = int(os.getenv("SCHEDULER_MAX_ATTEMPTS", "3"))
# Same shared secret api-gateway's /public/join already checks — no new secret.
JOIN_API_KEY = os.getenv("PUBLIC_BOT_API_KEY", "")


async def schedule_dispatch_loop() -> None:
    """Background loop: poll D1 `schedules` for due rows and dispatch them."""
    while True:
        try:
            await dispatch_due_schedules()
        except Exception as e:
            logger.error(f"Schedule dispatch loop error: {e}", exc_info=True)
        await asyncio.sleep(SCHEDULE_POLL_INTERVAL_SECONDS)


async def dispatch_due_schedules() -> int:
    """Dispatch every row with next_run <= now. Returns count dispatched."""
    now_ms = int(time.time() * 1000)
    rows = await select_due_schedules(now_ms)
    if rows is None:
        # D1 unreachable/unconfigured this tick — best-effort, try again next tick.
        return 0

    dispatched = 0
    for row in rows:
        try:
            await _dispatch_one(row, now_ms)
            dispatched += 1
        except Exception as e:
            logger.error(f"Failed to dispatch schedule {row.get('id')}: {e}", exc_info=True)
    return dispatched


async def _dispatch_one(row: dict[str, Any], now_ms: int) -> None:
    schedule_id = row["id"]
    recurrence = row["recurrence"]

    ok, detail = await _post_public_join(row)
    last_status = "sent" if ok else f"error {detail}"

    if recurrence != "once":
        next_run = next_recurring_run(
            now_ms,
            recurrence,
            row["time_zone"],
            int(row["hour"]),
            int(row["minute"]),
            int(row["weekday"]) if row.get("weekday") is not None else None,
        )
        await update_recurring_row(schedule_id, next_run=next_run, last_run=now_ms, last_status=last_status)
        return

    if ok:
        await delete_schedule_row(schedule_id)
        return

    attempts = int(row.get("attempts") or 0) + 1
    if attempts >= SCHEDULER_MAX_ATTEMPTS:
        await delete_schedule_row(schedule_id)
    else:
        # Leave next_run unchanged (not touched here) so the row stays due
        # and gets retried on the very next tick.
        await update_failed_attempt(schedule_id, last_run=now_ms, last_status=last_status, attempts=attempts)


async def _post_public_join(row: dict[str, Any]) -> tuple[bool, str]:
    """POST {API_BASE}/public/join for one due row. Returns (ok, detail)."""
    if not JOIN_API_KEY:
        return False, "PUBLIC_BOT_API_KEY not configured"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{SCHEDULER_API_BASE_URL}/public/join",
                json={"email": row["owner"], "meeting_url": row["meeting_url"]},
                headers={"X-API-Key": JOIN_API_KEY},
            )
        # 409 = meeting-api already has a bot in this meeting (dedup by
        # native_meeting_id) — treat as success, not a failure to retry.
        if resp.status_code in (200, 201, 409):
            return True, str(resp.status_code)
        logger.error(
            f"Schedule {row.get('id')}: /public/join returned {resp.status_code}: {resp.text[:300]}"
        )
        return False, str(resp.status_code)
    except httpx.RequestError as e:
        logger.error(f"Schedule {row.get('id')}: /public/join request error: {e}")
        return False, "request_error"
