"""D1 access for the `schedules` table — owned/auto-created by the Cloudflare
Worker side (dashboard writes rows there directly). This module only reads
due rows and updates their dispatch bookkeeping; it never alters the table's
schema.

Columns (Worker-defined): id TEXT pk, owner TEXT (email), meeting_url TEXT,
recurrence TEXT ('once'|'daily'|'weekdays'|'weekly'), time_zone TEXT (IANA),
hour INT, minute INT, weekday INT (0=Sun..6=Sat), next_run INT (epoch ms),
created_at INT (epoch ms), last_run INT (epoch ms), last_status TEXT,
attempts INT.

Enable via CLOUDFLARE_D1_ENABLED=true plus the CF_* credentials (config.py).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .config import CF_D1_SCHEDULES_TABLE
from .d1_client import d1_query

logger = logging.getLogger(__name__)

# A write immediately following a successful dispatch is the one write we
# really don't want to silently drop (see scheduler.py's crash-window note),
# so retry it a few times with a short backoff before giving up.
_WRITE_RETRIES = 3
_WRITE_RETRY_DELAY_SECONDS = 1.0


async def _write_with_retry(sql: str, params: list) -> bool:
    for attempt in range(1, _WRITE_RETRIES + 1):
        result = await d1_query(sql, params)
        if result is not None:
            return True
        if attempt < _WRITE_RETRIES:
            await asyncio.sleep(_WRITE_RETRY_DELAY_SECONDS)
    logger.error("D1 schedules write failed after %d attempts: %s", _WRITE_RETRIES, sql)
    return False


async def select_due_schedules(now_ms: int) -> Optional[list[dict[str, Any]]]:
    """Rows with next_run <= now_ms. Returns None if D1 is unreachable/unconfigured."""
    return await d1_query(
        f"SELECT * FROM {CF_D1_SCHEDULES_TABLE} WHERE next_run <= ?",
        [str(now_ms)],
    )


async def update_recurring_row(
    schedule_id: str, next_run: int, last_run: int, last_status: str
) -> bool:
    """Recurring row: advance next_run, reset attempts, record this run's outcome."""
    return await _write_with_retry(
        f"UPDATE {CF_D1_SCHEDULES_TABLE} "
        "SET next_run = ?, last_run = ?, last_status = ?, attempts = 0 WHERE id = ?",
        [str(next_run), str(last_run), last_status, schedule_id],
    )


async def update_failed_attempt(
    schedule_id: str, last_run: int, last_status: str, attempts: int
) -> bool:
    """One-time row that failed and hasn't hit the attempt cap: leave next_run
    unchanged (so it stays due and retries next tick), bump attempts."""
    return await _write_with_retry(
        f"UPDATE {CF_D1_SCHEDULES_TABLE} "
        "SET last_run = ?, last_status = ?, attempts = ? WHERE id = ?",
        [str(last_run), last_status, str(attempts), schedule_id],
    )


async def delete_schedule_row(schedule_id: str) -> bool:
    """One-time row that succeeded, or exhausted its retry budget."""
    return await _write_with_retry(
        f"DELETE FROM {CF_D1_SCHEDULES_TABLE} WHERE id = ?",
        [schedule_id],
    )


async def delete_schedules_by_owner(owner_email: str) -> bool:
    """Remove every schedule row owned by this email.

    Used when a client unsubscribes/disconnects — they should never
    auto-join a scheduled meeting again until they add a new one.
    """
    return await _write_with_retry(
        f"DELETE FROM {CF_D1_SCHEDULES_TABLE} WHERE owner = ?",
        [(owner_email or "").strip().lower()],
    )
