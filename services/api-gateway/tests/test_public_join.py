"""Tests for POST /public/join — the Cloudflare-BFF front door.

Verifies: a single call with {email, meeting_url} find-or-creates the user for
that email and launches a bot OWNED by them (per-client transcript isolation),
gated behind the system API key.
"""
import json
import pytest
import httpx
from httpx import ASGITransport
from unittest.mock import AsyncMock, MagicMock, patch

from main import app


def _make_client(admin_status=200, admin_user=None, bots_status=201, bots_body=None):
    """Mock app.state.http_client: .post = admin find-or-create, .request = /bots."""
    client = AsyncMock(spec=httpx.AsyncClient)

    admin_resp = MagicMock(spec=httpx.Response)
    admin_resp.status_code = admin_status
    admin_resp.json.return_value = admin_user or {"id": 7, "email": "client@acme.com", "max_concurrent_bots": 2}
    admin_resp.text = ""
    client.post = AsyncMock(return_value=admin_resp)

    bots_resp = MagicMock(spec=httpx.Response)
    bots_resp.status_code = bots_status
    bots_resp.content = json.dumps(bots_body or {"id": 99, "status": "requested"}).encode()
    bots_resp.headers = {"content-type": "application/json"}
    client.request = AsyncMock(return_value=bots_resp)

    return client


def _fwd_headers(client):
    """Headers passed to the meeting-api /bots forward call."""
    return client.request.call_args.kwargs["headers"]


@pytest.mark.asyncio
class TestPublicJoin:

    async def test_join_creates_user_and_owns_meeting(self):
        """Valid call → 201, find-or-create by email, bot owned by that user_id."""
        client = _make_client()
        app.state.http_client = client
        with patch("main.PUBLIC_BOT_API_KEY", "sys-key"), patch("main.ADMIN_API_TOKEN", "admin-key"):
            async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/public/join",
                    headers={"X-API-Key": "sys-key"},
                    json={"email": "Client@Acme.com", "meeting_url": "https://meet.google.com/abc-defg-hij"},
                )
        assert resp.status_code == 201
        # find-or-create called with the normalised (lowercased) email
        assert client.post.call_args.kwargs["json"]["email"] == "client@acme.com"
        # meeting forwarded owned by the resolved user, scoped to bot
        h = _fwd_headers(client)
        assert h["x-user-id"] == "7"
        assert h["x-user-scopes"] == "bot"

    async def test_join_requires_system_key(self):
        """Wrong/missing system key → 401, nothing forwarded."""
        client = _make_client()
        app.state.http_client = client
        with patch("main.PUBLIC_BOT_API_KEY", "sys-key"), patch("main.ADMIN_API_TOKEN", "admin-key"):
            async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/public/join",
                    headers={"X-API-Key": "wrong-key"},
                    json={"email": "c@acme.com", "meeting_url": "https://meet.google.com/abc-defg-hij"},
                )
        assert resp.status_code == 401
        client.request.assert_not_called()

    async def test_join_invalid_email(self):
        """Malformed email → 422."""
        client = _make_client()
        app.state.http_client = client
        with patch("main.PUBLIC_BOT_API_KEY", "sys-key"), patch("main.ADMIN_API_TOKEN", "admin-key"):
            async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/public/join",
                    headers={"X-API-Key": "sys-key"},
                    json={"email": "not-an-email", "meeting_url": "https://meet.google.com/abc-defg-hij"},
                )
        assert resp.status_code == 422
        client.request.assert_not_called()

    async def test_join_missing_meeting_link(self):
        """No meeting_url and no native_meeting_id → 422."""
        client = _make_client()
        app.state.http_client = client
        with patch("main.PUBLIC_BOT_API_KEY", "sys-key"), patch("main.ADMIN_API_TOKEN", "admin-key"):
            async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/public/join",
                    headers={"X-API-Key": "sys-key"},
                    json={"email": "c@acme.com"},
                )
        assert resp.status_code == 422

    async def test_join_user_resolution_failure(self):
        """admin-api unreachable / non-200 → 503, nothing forwarded."""
        client = _make_client(admin_status=503)
        app.state.http_client = client
        with patch("main.PUBLIC_BOT_API_KEY", "sys-key"), patch("main.ADMIN_API_TOKEN", "admin-key"):
            async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/public/join",
                    headers={"X-API-Key": "sys-key"},
                    json={"email": "c@acme.com", "meeting_url": "https://meet.google.com/abc-defg-hij"},
                )
        assert resp.status_code == 503
        client.request.assert_not_called()
