"""Tests for the per-meeting webhook override on POST /bots.

Covers the Cloudflare Worker / BFF integration shape: webhook config
passed on the JSON request body instead of relying on
``PUT /user/webhook``. Spec: implement-this.txt Part A + Part E.

Scenarios:
  * Body fields are accepted and stored on ``meeting.data``.
  * Body fields override gateway-header (user-level) defaults for THIS
    meeting only.
  * Old clients omitting the fields still work and still pick up the
    user-level config from gateway headers.
  * Malformed / SSRF-unsafe URLs are rejected with 422.
"""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import (
    TEST_CONTAINER_ID,
    TEST_CONTAINER_NAME,
    TEST_MEETING_ID,
    MockResult,
)


def _setup_create_meeting_db(mock_db):
    call_count = 0

    async def multi_execute(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return MockResult([])  # duplicate check
        elif call_count == 2:
            return MockResult(scalar_value=0)  # active-count
        return MockResult()

    mock_db.execute = AsyncMock(side_effect=multi_execute)


def _ctx(runtime_resp=None):
    if runtime_resp is None:
        runtime_resp = {"container_id": TEST_CONTAINER_ID, "name": TEST_CONTAINER_NAME}

    def factory():
        spawn_patch = patch(
            "meeting_api.meetings._spawn_via_runtime_api",
            new_callable=AsyncMock,
            return_value=runtime_resp,
        )
        mint_patch = patch(
            "meeting_api.meetings.mint_meeting_token", return_value="fake.jwt.token"
        )
        session_patch = patch("meeting_api.meetings.async_session_local")
        return spawn_patch, mint_patch, session_patch

    return factory


def _activate(factory):
    spawn_p, mint_p, sess_p = factory()
    spawn_p.start()
    mint_p.start()
    mock_sf = sess_p.start()
    inner = AsyncMock()
    inner.add = MagicMock()
    inner.commit = AsyncMock()
    mock_sf.return_value.__aenter__ = AsyncMock(return_value=inner)
    mock_sf.return_value.__aexit__ = AsyncMock(return_value=False)
    return [spawn_p, mint_p, sess_p]


def _stop(patches):
    for p in patches:
        p.stop()


class TestWebhookBodyOverride:

    @pytest.mark.asyncio
    async def test_body_webhook_config_accepted_and_stored(self, client, mock_db, mock_redis):
        """Body-supplied webhook_url/secret/events land on meeting.data."""
        _setup_create_meeting_db(mock_db)
        patches = _activate(_ctx())

        # Stub SSRF validator to permit a representative public URL.
        with patch(
            "meeting_api.webhook_url.validate_webhook_url",
            side_effect=lambda u: u,
        ):
            try:
                resp = await client.post("/bots", json={
                    "platform": "google_meet",
                    "native_meeting_id": "abc-defg-hij",
                    "webhook_url": "https://worker.example.com/webhooks/vexa",
                    "webhook_secret": "whsec_xyz",
                    "webhook_events": {
                        "meeting.completed": True,
                        "bot.failed": True,
                        "meeting.status_change": False,
                    },
                })
            finally:
                _stop(patches)

        assert resp.status_code == 201, resp.text
        # mock_db.add receives the Meeting ORM instance; inspect its .data
        added = [c.args[0] for c in mock_db.add.call_args_list]
        meetings = [m for m in added if hasattr(m, "data") and isinstance(m.data, dict)]
        assert meetings, "expected a Meeting object to be added"
        data = meetings[0].data
        assert data.get("webhook_url") == "https://worker.example.com/webhooks/vexa"
        assert data.get("webhook_secret") == "whsec_xyz"
        # Only positive opt-ins persisted
        assert data.get("webhook_events") == {
            "meeting.completed": True, "bot.failed": True,
        }

    @pytest.mark.asyncio
    async def test_body_overrides_gateway_user_default(self, client, mock_db, mock_redis):
        """Body webhook_url takes precedence over X-User-Webhook-* headers."""
        _setup_create_meeting_db(mock_db)
        patches = _activate(_ctx())

        with patch(
            "meeting_api.webhook_url.validate_webhook_url",
            side_effect=lambda u: u,
        ):
            try:
                resp = await client.post(
                    "/bots",
                    json={
                        "platform": "google_meet",
                        "native_meeting_id": "abc-defg-hij",
                        "webhook_url": "https://worker.example.com/hook",
                        "webhook_secret": "from_body",
                    },
                    headers={
                        # Gateway-injected defaults; simulate a user-level webhook.
                        "X-User-Webhook-URL": "https://user-default.example.com/hook",
                        "X-User-Webhook-Secret": "from_header",
                        "X-User-Webhook-Events": "meeting.completed",
                    },
                )
            finally:
                _stop(patches)

        assert resp.status_code == 201, resp.text
        added = [c.args[0] for c in mock_db.add.call_args_list]
        meetings = [m for m in added if hasattr(m, "data") and isinstance(m.data, dict)]
        data = meetings[0].data
        assert data["webhook_url"] == "https://worker.example.com/hook"
        # Secret must NOT leak across — body URL only gets the body secret.
        assert data["webhook_secret"] == "from_body"

    @pytest.mark.asyncio
    async def test_no_body_falls_back_to_gateway_headers(self, client, mock_db, mock_redis):
        """Old clients (no body fields) still pick up gateway-header defaults."""
        _setup_create_meeting_db(mock_db)
        patches = _activate(_ctx())

        try:
            resp = await client.post(
                "/bots",
                json={"platform": "google_meet", "native_meeting_id": "abc-defg-hij"},
                headers={
                    "X-User-Webhook-URL": "https://user-default.example.com/hook",
                    "X-User-Webhook-Secret": "user_secret",
                    "X-User-Webhook-Events": "meeting.completed,bot.failed",
                },
            )
        finally:
            _stop(patches)

        assert resp.status_code == 201, resp.text
        added = [c.args[0] for c in mock_db.add.call_args_list]
        meetings = [m for m in added if hasattr(m, "data") and isinstance(m.data, dict)]
        data = meetings[0].data
        assert data["webhook_url"] == "https://user-default.example.com/hook"
        assert data["webhook_secret"] == "user_secret"
        assert data["webhook_events"] == {"meeting.completed": True, "bot.failed": True}

    @pytest.mark.asyncio
    async def test_no_body_no_headers_still_works(self, client, mock_db, mock_redis):
        """Backward compat: no webhook anywhere → 201, no webhook_* keys stored."""
        _setup_create_meeting_db(mock_db)
        patches = _activate(_ctx())

        try:
            resp = await client.post("/bots", json={
                "platform": "google_meet",
                "native_meeting_id": "abc-defg-hij",
            })
        finally:
            _stop(patches)

        assert resp.status_code == 201, resp.text
        added = [c.args[0] for c in mock_db.add.call_args_list]
        meetings = [m for m in added if hasattr(m, "data") and isinstance(m.data, dict)]
        data = meetings[0].data
        assert "webhook_url" not in data
        assert "webhook_secret" not in data
        assert "webhook_events" not in data


class TestWebhookBodyValidation:

    @pytest.mark.asyncio
    async def test_ssrf_url_rejected(self, client, mock_db, mock_redis):
        """A webhook_url that the SSRF validator rejects → 422."""
        _setup_create_meeting_db(mock_db)
        patches = _activate(_ctx())

        try:
            resp = await client.post("/bots", json={
                "platform": "google_meet",
                "native_meeting_id": "abc-defg-hij",
                # localhost — blocked by hostname allowlist in webhook_url.py
                "webhook_url": "http://localhost/hook",
            })
        finally:
            _stop(patches)

        assert resp.status_code == 422, resp.text
        # Message comes from validate_webhook_url, surfaced via HTTPException.
        body = resp.json()
        assert "webhook_url" in (body.get("detail") or "").lower() or \
            re.search(r"internal|private|webhook", str(body).lower())

    @pytest.mark.asyncio
    async def test_body_url_without_secret_does_not_inherit_header_secret(
        self, client, mock_db, mock_redis,
    ):
        """When body provides webhook_url but no secret, we do NOT mix in the
        header secret (which belongs to a different URL)."""
        _setup_create_meeting_db(mock_db)
        patches = _activate(_ctx())

        with patch(
            "meeting_api.webhook_url.validate_webhook_url",
            side_effect=lambda u: u,
        ):
            try:
                resp = await client.post(
                    "/bots",
                    json={
                        "platform": "google_meet",
                        "native_meeting_id": "abc-defg-hij",
                        "webhook_url": "https://worker.example.com/hook",
                        # no webhook_secret on body
                    },
                    headers={
                        "X-User-Webhook-URL": "https://user-default.example.com/hook",
                        "X-User-Webhook-Secret": "header_secret_for_different_url",
                    },
                )
            finally:
                _stop(patches)

        assert resp.status_code == 201, resp.text
        added = [c.args[0] for c in mock_db.add.call_args_list]
        meetings = [m for m in added if hasattr(m, "data") and isinstance(m.data, dict)]
        data = meetings[0].data
        assert data["webhook_url"] == "https://worker.example.com/hook"
        # The header-supplied secret belongs to the header URL; it must not
        # be silently carried over onto the body URL.
        assert data.get("webhook_secret") != "header_secret_for_different_url"
