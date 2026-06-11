# Cloudflare D1 mirror

Two best-effort mirrors run against the same D1 database:

1. **Transcript segments** — every finalized segment Vexa writes to Postgres is
   also forwarded to D1. Hooks into `process_redis_to_postgres`
   (`services/meeting-api/meeting_api/collector/db_writer.py`) and inherits the
   immutability window, speaker mapping, and `segment_id` dedupe.
2. **Meeting state** — meeting metadata + lifecycle state (status,
   timestamps, completion_reason, failure_stage, segment_count). Hooks into
   `POST /bots`, the bot callback handlers, and the post-meeting task runner
   so Cloudflare-side dashboards can answer "what meetings exist for a user,
   what is their status, basic metadata, terminal outcome" without round-
   tripping to AWS for every read. Implementation:
   `services/meeting-api/meeting_api/collector/d1_meeting_forwarder.py`.

Both are **best-effort**: if D1 is unreachable, rejects the write, or is
misconfigured, Postgres stays authoritative and meeting creation /
transcription / callbacks are unaffected (the error is logged and skipped).

**Recordings are NOT mirrored.** Cloudflare callers fetch recording metadata
and download URLs directly from the AWS REST endpoints under `/recordings/*`.
See `docs/cloudflare-worker-integration.md`.

**Secrets are NOT mirrored.** The meeting forwarder explicitly excludes
`webhook_url`, `webhook_secret`, `webhook_events` and any other credentials
from the snapshot.

## One-time setup

1. **Create a D1 database** (Cloudflare dashboard → Workers & Pages → D1, or
   `wrangler d1 create vexa-transcripts`). Note the **database ID** and your
   **account ID**.

2. **Apply both schemas:**
   ```bash
   wrangler d1 execute vexa-transcripts --remote --file deploy/cloudflare-d1/schema.sql
   wrangler d1 execute vexa-transcripts --remote --file deploy/cloudflare-d1/schema_meetings.sql
   ```

3. **Create an API token** (My Profile → API Tokens) with the **D1 Edit**
   permission for your account.

4. **Set env vars** in the repo-root `.env` (the compose `meeting-api` service reads them):
   ```bash
   CLOUDFLARE_D1_ENABLED=true
   CF_ACCOUNT_ID=<your-account-id>
   CF_D1_DATABASE_ID=<your-d1-database-id>
   CF_API_TOKEN=<your-d1-api-token>
   CF_D1_TABLE=transcriptions          # optional, this is the default
   CF_D1_MEETINGS_TABLE=meetings       # optional, this is the default
   ```

5. **Rebuild & restart meeting-api:**
   ```bash
   cd deploy/compose && docker compose up -d --build meeting-api
   ```

## Verify

```bash
# Watch the forward happen (after a bot transcribes a meeting):
docker compose logs -f meeting-api | grep -Ei "Stored .* to PostgreSQL|Mirrored .* to Cloudflare D1|D1 forward"

# Confirm rows landed in D1:
wrangler d1 execute vexa-transcripts --remote \
  --command "SELECT count(*) AS n, max(created_at) AS latest FROM transcriptions"
```

Re-running a meeting (or the periodic immutability flush) must not grow the row count
beyond the distinct segments — the upsert dedupes on `(meeting_id, segment_id)`.

## Disable

Set `CLOUDFLARE_D1_ENABLED=false` (or remove it) and restart `meeting-api`. The hook
becomes a no-op; nothing else changes.

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `CLOUDFLARE_D1_ENABLED` | `false` | Master switch. When false the forward is a no-op. |
| `CF_ACCOUNT_ID` | — | Cloudflare account ID. |
| `CF_D1_DATABASE_ID` | — | Target D1 database ID. |
| `CF_API_TOKEN` | — | API token with D1 Edit permission. |
| `CF_D1_TABLE` | `transcriptions` | Transcript destination table name. |
| `CF_D1_MEETINGS_TABLE` | `meetings` | Meeting-state destination table name. |
| `CF_D1_TIMEOUT_SECONDS` | `10` | HTTP timeout per D1 request. |
