"""Tests for the Cloudflare D1 meeting-state mirror.

Spec: implement-this.txt Part B + Part E + Part F.

Asserts:
  * ``build_snapshot`` exposes the expected typed columns and explicitly
    EXCLUDES secrets (webhook_url, webhook_secret, webhook_events).
  * The forwarder is a no-op when D1 is disabled / unconfigured.
  * The forwarder swallows HTTP and unexpected errors — D1 failures must
    NEVER raise into the primary flow (meeting creation, callbacks,
    post-meeting tasks).
  * The upsert SQL is keyed on meeting_id and refreshes mutable columns on
    conflict so re-sent snapshots overwrite (not duplicate).
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from .conftest import (
    TEST_MEETING_ID,
    TEST_NATIVE_MEETING_ID,
    TEST_PLATFORM,
    TEST_USER_ID,
    make_meeting,
)


class TestBuildSnapshot:
    def test_snapshot_columns_and_typed_hoists(self):
        from meeting_api.collector.d1_meeting_forwarder import build_snapshot

        now = datetime(2026, 6, 11, 10, 0, 0)
        meeting = make_meeting(
            id=TEST_MEETING_ID,
            user_id=TEST_USER_ID,
            platform=TEST_PLATFORM,
            platform_specific_id=TEST_NATIVE_MEETING_ID,
            status="completed",
            start_time=now,
            end_time=now,
            created_at=now,
            updated_at=now,
            data={
                "name": "Weekly catchup call",
                "bot_name": "Acme Notetaker",
                "language": "en",
                "transcribe_enabled": True,
                "recording_enabled": False,
                "segment_count": 197,
                "completion_reason": "stopped",
                "failure_stage": None,
                # Things that MUST NOT appear in the snapshot:
                "webhook_url": "https://worker.example.com/hook",
                "webhook_secret": "whsec_xyz",
                "webhook_events": {"meeting.completed": True},
                "passcode": "1234",
                "meeting_url": "https://meet.google.com/abc-defg-hij",
            },
        )

        snap = build_snapshot(meeting)
        assert snap["meeting_id"] == TEST_MEETING_ID
        assert snap["user_id"] == TEST_USER_ID
        assert snap["platform"] == TEST_PLATFORM
        assert snap["native_meeting_id"] == TEST_NATIVE_MEETING_ID
        assert snap["status"] == "completed"
        # The display title — D1 is the frontend's only read source, so without
        # this it can only show the raw Meet code.
        assert snap["name"] == "Weekly catchup call"
        assert snap["bot_name"] == "Acme Notetaker"
        assert snap["language"] == "en"
        assert snap["transcribe_enabled"] == 1
        assert snap["recording_enabled"] == 0
        assert snap["segment_count"] == 197
        assert snap["completion_reason"] == "stopped"
        assert snap["failure_stage"] is None
        # datetime → ISO string
        assert isinstance(snap["created_at"], str) and "2026" in snap["created_at"]

        # No secrets, no surprise fields. Lock the column set in place.
        expected_keys = {
            "meeting_id", "user_id", "platform", "name", "native_meeting_id",
            "status", "bot_name", "language",
            "transcribe_enabled", "recording_enabled",
            "segment_count", "started_at", "ended_at",
            "created_at", "updated_at",
            "completion_reason", "failure_stage",
        }
        assert set(snap.keys()) == expected_keys
        # And no fragment of any secret leaked through.
        assert "whsec_xyz" not in str(snap)
        assert "worker.example.com" not in str(snap)
        assert "passcode" not in snap
        assert "1234" not in str(snap)

    def test_name_falls_back_to_title_then_none(self):
        """`name` mirrors the dashboard's own resolution order (meeting-card.tsx):
        data.name, else data.title, else nothing to show."""
        from meeting_api.collector.d1_meeting_forwarder import build_snapshot
        from meeting_api.models import Meeting

        now = datetime(2026, 5, 1, 12, 0, 0)

        def _m(data):
            return Meeting(
                id=TEST_MEETING_ID, user_id=TEST_USER_ID, platform=TEST_PLATFORM,
                platform_specific_id=TEST_NATIVE_MEETING_ID, status="completed",
                start_time=now, end_time=now, created_at=now, updated_at=now,
                data=data,
            )

        assert build_snapshot(_m({"name": "Standup", "title": "Ignored"}))["name"] == "Standup"
        assert build_snapshot(_m({"title": "From calendar"}))["name"] == "From calendar"
        # An unnamed meeting (e.g. a bare /public/join) mirrors NULL, and the
        # frontend falls back to the Meet code.
        assert build_snapshot(_m({}))["name"] is None

    def test_snapshot_handles_minimal_meeting(self):
        from meeting_api.collector.d1_meeting_forwarder import build_snapshot

        meeting = make_meeting(data={})
        snap = build_snapshot(meeting)
        assert snap["meeting_id"] == TEST_MEETING_ID
        assert snap["bot_name"] is None
        assert snap["language"] is None
        assert snap["transcribe_enabled"] is None
        assert snap["recording_enabled"] is None
        assert snap["segment_count"] is None
        assert snap["completion_reason"] is None
        assert snap["failure_stage"] is None


class TestUpsertSql:
    def test_upsert_keyed_on_meeting_id_and_refreshes_mutables(self):
        from meeting_api.collector.d1_meeting_forwarder import (
            _build_upsert, build_snapshot, _COLUMNS,
        )
        meeting = make_meeting(data={"transcribe_enabled": True, "segment_count": 5})
        snap = build_snapshot(meeting)
        chunk = _build_upsert(snap)
        sql = chunk["sql"].lower()
        assert "insert into" in sql
        assert "on conflict (meeting_id) do update" in sql
        # Mutable columns get refreshed on conflict — including `name`, so a
        # dashboard rename or a late calendar title propagates to D1.
        for col in ("status", "segment_count", "updated_at", "completion_reason", "name"):
            assert f"{col}=excluded.{col}" in sql, f"missing refresh of {col}"
        # The key column must NOT be refreshed
        assert "meeting_id=excluded.meeting_id" not in sql
        # created_at must NOT be overwritten by later snapshots
        assert "created_at=excluded.created_at" not in sql
        # Params arity tracks the column set (derived, so adding a column can't
        # silently desync the bound-parameter count).
        assert len(chunk["params"]) == len(_COLUMNS)


class TestForwarderBestEffort:
    @pytest.mark.asyncio
    async def test_disabled_is_no_op(self):
        """When CLOUDFLARE_D1_ENABLED is false the forwarder must not call out."""
        from meeting_api.collector import d1_meeting_forwarder as mod

        with patch.object(mod, "CLOUDFLARE_D1_ENABLED", False), \
             patch("httpx.AsyncClient") as mock_http:
            await mod.forward_meeting_to_d1(make_meeting())
            mock_http.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_credentials_is_no_op(self):
        """Enabled but missing credentials → log warning, return cleanly."""
        from meeting_api.collector import d1_meeting_forwarder as mod

        with patch.object(mod, "CLOUDFLARE_D1_ENABLED", True), \
             patch.object(mod, "CF_ACCOUNT_ID", ""), \
             patch("httpx.AsyncClient") as mock_http:
            await mod.forward_meeting_to_d1(make_meeting())
            mock_http.assert_not_called()

    @pytest.mark.asyncio
    async def test_http_failure_is_swallowed(self):
        """A network error inside the forwarder must NOT propagate.

        This is the load-bearing invariant: meeting creation, callbacks and
        post-meeting tasks all call the mirror; any raise here would break
        the primary flow.
        """
        from meeting_api.collector import d1_meeting_forwarder as mod

        with patch.object(mod, "CLOUDFLARE_D1_ENABLED", True), \
             patch.object(mod, "CF_ACCOUNT_ID", "acct"), \
             patch.object(mod, "CF_D1_DATABASE_ID", "db"), \
             patch.object(mod, "CF_API_TOKEN", "tok"):
            failing_client = AsyncMock()
            failing_client.__aenter__ = AsyncMock(return_value=failing_client)
            failing_client.__aexit__ = AsyncMock(return_value=False)
            failing_client.post = AsyncMock(
                side_effect=httpx.ConnectError("boom"),
            )
            with patch("httpx.AsyncClient", return_value=failing_client):
                # Must complete without raising.
                await mod.forward_meeting_to_d1(make_meeting())

    @pytest.mark.asyncio
    async def test_non_200_response_is_swallowed(self):
        from meeting_api.collector import d1_meeting_forwarder as mod

        with patch.object(mod, "CLOUDFLARE_D1_ENABLED", True), \
             patch.object(mod, "CF_ACCOUNT_ID", "acct"), \
             patch.object(mod, "CF_D1_DATABASE_ID", "db"), \
             patch.object(mod, "CF_API_TOKEN", "tok"):
            bad_resp = MagicMock(status_code=500, text="boom")
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            client.post = AsyncMock(return_value=bad_resp)
            with patch("httpx.AsyncClient", return_value=client):
                await mod.forward_meeting_to_d1(make_meeting())  # must not raise

    @pytest.mark.asyncio
    async def test_success_path_sends_one_request_keyed_on_meeting_id(self):
        from meeting_api.collector import d1_meeting_forwarder as mod

        ok_resp = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"success": True, "result": []}),
        )
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(return_value=ok_resp)

        with patch.object(mod, "CLOUDFLARE_D1_ENABLED", True), \
             patch.object(mod, "CF_ACCOUNT_ID", "acct"), \
             patch.object(mod, "CF_D1_DATABASE_ID", "db"), \
             patch.object(mod, "CF_API_TOKEN", "tok"), \
             patch("httpx.AsyncClient", return_value=client):
            await mod.forward_meeting_to_d1(make_meeting(data={"segment_count": 5}))

        assert client.post.await_count == 1
        # Sent the upsert with meeting_id as first param
        call_kwargs = client.post.await_args.kwargs
        body = call_kwargs["json"]
        assert "on conflict (meeting_id)" in body["sql"].lower()
        assert body["params"][0] == str(TEST_MEETING_ID)

    @pytest.mark.asyncio
    async def test_none_meeting_is_no_op(self):
        """Safety guard: a missing meeting should not crash the forwarder."""
        from meeting_api.collector import d1_meeting_forwarder as mod

        with patch.object(mod, "CLOUDFLARE_D1_ENABLED", True):
            await mod.forward_meeting_to_d1(None)  # must not raise
