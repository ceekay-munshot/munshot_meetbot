-- Cloudflare D1 schema for the Vexa meeting-state mirror.
-- Companion to schema.sql (which mirrors transcript segments).
--
-- Apply once, e.g.:
--   wrangler d1 execute <DB_NAME> --remote --file deploy/cloudflare-d1/schema_meetings.sql
-- or paste into the D1 console.
--
-- The PRIMARY KEY on meeting_id matches the upsert
-- (INSERT ... ON CONFLICT (meeting_id) DO UPDATE) emitted by
-- services/meeting-api/meeting_api/collector/d1_meeting_forwarder.py.
-- AWS Postgres remains the source of truth; this table is a best-effort
-- mirror for Cloudflare-side dashboard reads (status, lifecycle
-- timestamps, segment counts, terminal classification).
--
-- IMPORTANT: secrets must NEVER land here. The forwarder explicitly
-- excludes webhook_url, webhook_secret, webhook_events and any other
-- credentials from the snapshot.

CREATE TABLE IF NOT EXISTS meetings (
  meeting_id          INTEGER PRIMARY KEY,
  user_id             INTEGER NOT NULL,
  platform            TEXT    NOT NULL,
  native_meeting_id   TEXT,
  status              TEXT    NOT NULL,
  bot_name            TEXT,
  language            TEXT,
  transcribe_enabled  INTEGER,
  recording_enabled   INTEGER,
  segment_count       INTEGER,
  started_at          TEXT,
  ended_at            TEXT,
  created_at          TEXT,
  updated_at          TEXT,
  completion_reason   TEXT,
  failure_stage       TEXT
);

CREATE INDEX IF NOT EXISTS ix_meetings_user_created
  ON meetings (user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_meetings_user_status
  ON meetings (user_id, status);
CREATE INDEX IF NOT EXISTS ix_meetings_platform_native
  ON meetings (platform, native_meeting_id);
