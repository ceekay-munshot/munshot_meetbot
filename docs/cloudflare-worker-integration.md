# Cloudflare Worker / BFF integration

This document describes the contract between a Cloudflare Worker (or any
edge BFF) and the AWS-hosted Vexa stack in this repo. It is **not** an
implementation of the Worker itself — the Worker lives outside this repo.

## Architecture

```
┌────────────────────┐    ┌─────────────────────┐    ┌──────────────────────┐
│ Cloudflare Pages   │    │ Cloudflare Worker   │    │ AWS-hosted Vexa      │
│ (user-facing UI)   │───▶│ (BFF / public edge) │───▶│ - api-gateway        │
└────────────────────┘    └─────────────────────┘    │ - meeting-api        │
        ▲                          │                 │ - transcription-     │
        │                          │                 │   collector          │
        │                          │                 │ - Postgres + Redis   │
        │                          │                 │ - S3 / MinIO         │
        │                          │                 └──────────────────────┘
        │                          │                            │
        │                          │ webhooks                   │ best-effort mirror
        │                          ▼                            ▼
        │                  ┌─────────────────┐         ┌────────────────────┐
        └──────────────────│ Cloudflare D1   │◀────────│  (mirror writer)   │
                           │ - transcriptions│         └────────────────────┘
                           │ - meetings      │
                           └─────────────────┘
```

AWS is the **system of record** for bots, live transcripts, meeting state,
recordings, and bot lifecycle. D1 is a **best-effort read mirror** for
finalized transcript segments and meeting-level state — useful for
Cloudflare-side archive/search pages and for "what meetings exist for this
user" reads without an AWS round-trip.

**Strong recommendation:** live UX (an in-progress meeting, the live
transcript scrolling on screen) should read AWS directly via REST + WS.
D1 should be treated as mirrored persistence, search, archive, or
fallback read model — not the live source of truth.

## 1. Worker → AWS REST

All REST calls go through the API gateway. Auth: `X-API-Key: <token>`.

### `POST /bots`
Create a bot for a meeting. Schema:
`services/meeting-api/meeting_api/schemas.py :: MeetingCreate`.

Per-meeting webhook config can be supplied directly on the JSON body —
no prior `PUT /user/webhook` required:

```json
{
  "meeting_url": "https://meet.google.com/abc-defg-hij",
  "bot_name": "Acme Notetaker",
  "language": "en",
  "transcribe_enabled": true,
  "webhook_url": "https://my-worker.example.com/webhooks/vexa",
  "webhook_secret": "whsec_...",
  "webhook_events": {
    "meeting.started": true,
    "meeting.completed": true,
    "bot.failed": true,
    "meeting.status_change": false,
    "recording.completed": true
  }
}
```

Resolution order: per-meeting body fields → user-level default
(`PUT /user/webhook`) → no webhook. The body URL is SSRF-validated before
being stored on `meeting.data`; invalid URLs return 422 with a clear
message. Webhook **secrets** are never mirrored to D1 — they live only in
the AWS Postgres `meetings.data` row and are used by AWS to sign webhook
deliveries to the Worker.

### `GET /meetings`, `GET /meetings/{id}`
List meetings or fetch one. Use these for the canonical view; D1 is a
mirror, not a write-through cache.

### `GET /transcripts/{platform}/{native_meeting_id}`
Bootstrap a transcript before opening a live WS subscription. The response
contains the existing finalized + mutable segments.

### `POST /meetings/{meeting_id}/transcribe`
Trigger deferred transcription for a completed meeting that has a
recording but no live transcript. After this completes, the D1 meeting
mirror is updated with the new `segment_count`.

### `/recordings/*`
Recording metadata + downloads are **NOT** mirrored to D1. Always fetch
them from AWS directly:

- `GET /recordings`
- `GET /recordings/{recording_id}`
- `GET /recordings/{recording_id}/master?type=audio|video`
- `GET /recordings/{recording_id}/media/{media_file_id}/download`
- `GET /recordings/{recording_id}/media/{media_file_id}/raw`
- `DELETE /recordings/{recording_id}`

## 2. Worker → AWS WebSocket

Connect:

```
wss://<gateway-host>/ws?api_key=<token>
```

Subscribe (client → server):

```json
{
  "action": "subscribe",
  "meetings": [
    { "platform": "google_meet", "native_id": "abc-defg-hij" }
  ]
}
```

Message classes (server → client):

| `type`                 | meaning                                                       |
|------------------------|---------------------------------------------------------------|
| `subscribed`           | acknowledgement of the subscribe payload                      |
| `transcript.mutable`   | in-flight segment update; replace any previous mutable segment |
| `transcript.finalized` | immutable segment; safe to persist locally                    |
| `meeting.status`       | bot/meeting status transition                                 |
| `pong`                 | response to a client `ping`                                   |
| `error`                | malformed subscription or other protocol error                |

Channels are keyed by `meeting_id`. The collector fans transcript + status
+ chat messages into them; the Worker forwards what its UI needs.

## 3. AWS → Worker webhooks

AWS POSTs webhook deliveries to the URL configured on the meeting (body
override) or the user (PUT /user/webhook fallback). Envelope shape, built
by `meeting_api.webhook_delivery.build_envelope`:

```json
{
  "event_id":   "evt_<uuid>",
  "event_type": "meeting.completed",
  "api_version": "2026-03-01",
  "created_at": "2026-06-11T10:00:00+00:00",
  "data":       { ... }
}
```

### Signature verification

Two headers are always present when a secret is configured:

| Header                | Format                          | Notes |
|-----------------------|---------------------------------|-------|
| `Authorization`       | `Bearer <secret>`               | Backwards-compatible bearer scheme. |
| `X-Webhook-Timestamp` | unix-seconds integer            | Use for replay-window enforcement (e.g. reject if `now - ts > 300s`). |
| `X-Webhook-Signature` | `sha256=<hex>`                  | HMAC-SHA256 over `"{timestamp}.{rawBody}"` using the per-meeting secret. |

Verify on the Worker by recomputing `sha256(secret, ts + "." + rawBody)`
and comparing it to the `sha256=` payload in `X-Webhook-Signature` in
constant time. Reject if missing, mismatched, or outside the replay
window.

### Event types

| `event_type`              | When fired                                                   |
|---------------------------|--------------------------------------------------------------|
| `meeting.started`         | bot reports active in the meeting                            |
| `meeting.completed`       | terminal completion (whether stopped, ended, or timed-out)   |
| `bot.failed`              | terminal failure path                                        |
| `meeting.status_change`   | any other lifecycle transition (joining, awaiting_admission) |
| `recording.completed`     | recording finalization finished                              |

Filter with the `webhook_events` map on the meeting (or user). Events
absent or `false` are not delivered.

## 4. D1 contract

Two tables, both managed by best-effort mirrors that **never block** the
primary AWS flow if D1 is unreachable.

### `transcriptions`
Mirror of finalized transcript segments. Schema:
`deploy/cloudflare-d1/schema.sql`. Idempotent on
`(meeting_id, segment_id)`. Useful for Cloudflare-side archive/search
pages where eventual consistency is fine. Live transcript UX should
still use the WS `transcript.mutable` / `transcript.finalized` stream.

### `meetings`
Mirror of meeting-level state. Schema:
`deploy/cloudflare-d1/schema_meetings.sql`. Upserted on:

- meeting creation (POST /bots)
- bot lifecycle transitions (joining, awaiting_admission, active, terminal)
- post-meeting finalization (after aggregation populates segment_count)
- deferred transcription completion

Columns (no secrets, ever):

```
meeting_id, user_id, platform, native_meeting_id,
status, bot_name, language,
transcribe_enabled, recording_enabled,
segment_count,
started_at, ended_at, created_at, updated_at,
completion_reason, failure_stage
```

`webhook_url`, `webhook_secret`, and `webhook_events` are explicitly
excluded from the snapshot — they stay on AWS Postgres only.

### What D1 is for / not for

| Use D1 for                                  | Use AWS for                                |
|---------------------------------------------|--------------------------------------------|
| "What meetings does this user have?"        | Live in-progress meeting view              |
| Search / archive / past-meeting list pages  | Live transcript scroll                     |
| Cloudflare-only fallback read model         | Recording metadata + downloads             |
| Quick "is the transcript ready?" check      | Initiating bots                            |
|                                             | Anything mutating                          |

## 5. Cloudflare-side usage model

1. User clicks "join" on Cloudflare Pages.
2. Worker calls `POST /bots` on AWS, supplying its own
   `webhook_url` / `webhook_secret` for delivery back to the Worker.
3. Worker stores the returned `meeting_id` and platform/native id
   mapping in its own app DB if it wants edge-local state.
4. Cloudflare UI uses either:
   - direct Worker proxy to AWS `GET /transcripts/...` + `WS /ws` for
     live view, or
   - D1 for delayed / finalized read pages.
5. AWS delivers webhook events to the Worker as lifecycle progresses.
6. Worker may verify signature, persist event-derived state into D1 or
   its own app tables, and surface notifications to the user.
