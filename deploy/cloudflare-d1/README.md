# Cloudflare D1 transcript mirror

Forwards every **finalized** transcript segment that Vexa writes to Postgres into a
Cloudflare D1 database as well. It hooks into the existing
`process_redis_to_postgres` loop (`services/meeting-api/meeting_api/collector/db_writer.py`),
so it inherits Vexa's immutability window, speaker mapping, and `segment_id`
deduplication. The forward is **best-effort**: if D1 is unreachable or rejects the
write, Postgres stays authoritative and the meeting is unaffected (the error is logged
and skipped).

## One-time setup

1. **Create a D1 database** (Cloudflare dashboard → Workers & Pages → D1, or
   `wrangler d1 create vexa-transcripts`). Note the **database ID** and your
   **account ID**.

2. **Apply the schema:**
   ```bash
   wrangler d1 execute vexa-transcripts --remote --file deploy/cloudflare-d1/schema.sql
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
| `CF_D1_TABLE` | `transcriptions` | Destination table name. |
| `CF_D1_TIMEOUT_SECONDS` | `10` | HTTP timeout per D1 request. |
