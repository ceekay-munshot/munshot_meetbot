"""Generic Cloudflare D1 REST client — read + write.

Companion to the existing write-only forwarders (``d1_forwarder.py``,
``d1_meeting_forwarder.py``, ``d1_owners_forwarder.py``), which each hand-roll
their own fire-and-forget INSERT statement. This module adds the one
capability none of them need: parsing rows back out of a D1 response, for
callers that must SELECT (the schedules dispatcher, the calendar_events
reader) or run an UPDATE/DELETE outside the segment-mirror shape. Still
best-effort/never-raise — callers get ``None`` on any failure and decide how
to handle it (skip this tick, log, etc.).

Enable via CLOUDFLARE_D1_ENABLED=true plus the CF_* credentials (see config.py).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from .config import (
    CLOUDFLARE_D1_ENABLED,
    CF_ACCOUNT_ID,
    CF_D1_DATABASE_ID,
    CF_API_TOKEN,
    CF_D1_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)


def _d1_query_url() -> str:
    return (
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
        f"/d1/database/{CF_D1_DATABASE_ID}/query"
    )


def is_configured() -> bool:
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
            "Cloudflare D1 access enabled but missing config: %s. Skipping.",
            ", ".join(missing),
        )
        return False
    return True


async def d1_query(sql: str, params: Optional[list] = None) -> Optional[list[dict[str, Any]]]:
    """Run a single D1 statement and return its result rows.

    Returns a list of row dicts for a SELECT (empty list if nothing matched),
    an empty list for a successful INSERT/UPDATE/DELETE (this API doesn't
    report affected-row counts), or ``None`` if D1 is unconfigured or the
    request/statement failed. Never raises.
    """
    if not is_configured():
        return None

    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=CF_D1_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                _d1_query_url(), headers=headers, json={"sql": sql, "params": params or []}
            )
        if resp.status_code != 200:
            logger.error("D1 query HTTP %s: %s", resp.status_code, resp.text[:500])
            return None
        body = resp.json()
        if not body.get("success", False):
            logger.error("D1 query returned errors: %s", body.get("errors"))
            return None
        result = body.get("result") or []
        if not result:
            return []
        return result[0].get("results") or []
    except httpx.RequestError as e:
        logger.error("D1 query request error (non-fatal): %s", e)
        return None
    except Exception as e:  # noqa: BLE001 - client must never propagate
        logger.error("D1 query unexpected error (non-fatal): %s", e, exc_info=True)
        return None
