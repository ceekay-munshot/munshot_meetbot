# munshot meetbot (otter_clonw) — Product Overview

A **Google-Meet-only fork of Vexa**: a multi-tenant SaaS that sends a bot into a
Google Meet, records the meeting, and produces a speaker-labelled transcript.
Productized as **Cloudflare frontend → AWS backend**.

---

## 1. What it does (product scope)

- A client asks for a bot → a headless Chromium bot joins their Google Meet as
  **"munshot meetbot"**.
- The bot **records the whole meeting** (audio + who-spoke-when timeline).
- When the meeting ends, the **entire audio is transcribed in one batch** by
  Deepgram (multi-language + diarization) — not live, on purpose.
- The finished transcript is stored in Postgres and **mirrored to Cloudflare D1**,
  where the customer-facing frontend reads it, filtered per client.
- **Calendar auto-join**: a client connects Google Calendar once; the system then
  auto-sends a bot to every scheduled Meet.

Multi-tenant: every meeting/transcript is owned by a user (`owner_email`), with
per-user concurrency caps.

---

## 2. Tech stack

| Layer | Technology |
|---|---|
| Backend services | Python **FastAPI**, async **SQLAlchemy**, Alembic migrations |
| The bot | **TypeScript / Node**, **Playwright**, **Chromium**, browser `MediaRecorder` |
| Transcription | **Deepgram `nova-3`** (batch, multi-lang, diarized) + **Groq** — *cloud, no GPU* |
| Datastore | **Postgres 17** (system of record), **Redis 7** (bus), **MinIO** (S3 recordings) |
| Orchestration | **Docker Compose**; bots are spawned as ephemeral Docker containers |
| Frontend / edge | **Cloudflare** Workers/Pages + **D1** (SQLite); Next.js dashboard (upstream) |
| Deploy target | **AWS EC2** single box + compose (no GPU, no k8s needed at this scale) |

--
## 3. Services (10 containers)-


| Service | Port | Role |
|---|---|---|
| **api-gateway** | 8056→8000 | Public edge. Auth, rate-limit, proxy. Hosts `/public/google-meet`, `/public/join`, `/bots`, `/calendar/*` |
| **meeting-api** | 8080 (internal) | **The brain.** Bot lifecycle, launches bot containers, receives callbacks, orchestrates batch transcription, webhooks, D1 mirror |
| **admin-api** | 8057→8001 | User & API-token CRUD; resolves an API key → `{user_id, scopes, max_concurrent}` |
| **runtime-api** | 8090 | Scheduler: calendar auto-join jobs + bot timeout jobs |
| **calendar-service** | 8050 | Google Calendar OAuth + event sync → schedules auto-joins |
| **vexa-bot** | (ephemeral) | Per-meeting Chromium bot. Joins Meet, records audio, samples active speaker |
| **transcription-service** | 8000 (internal) | Thin wrapper over Deepgram/Groq cloud APIs. Batch + realtime modes |
| **dashboard** | 3001→3000 | Next.js UI (mostly upstream; the real frontend is on Cloudflare) |
| **postgres** | 5458→5432 | System of record: meetings, transcriptions, users, tokens, calendar |
| **redis** | 6379 | Pub/sub bus, bot status, rate limiting, durable container-stop outbox |
| **minio** | 9000/9001 | S3-compatible store for recording chunks + assembled audio |

---

## 4. End-to-end flow (the main path)

```
                        CLOUDFLARE EDGE (customer-facing)
   ┌──────────────────────────────────────────────────────────────────┐
   │  Frontend (Workers/Pages)            D1 (SQLite mirror of          │
   │   • POST bot request                  transcripts, per owner_email)│
   │   • reads transcripts ◄───────────────────────┐                    │
   └─────────┬──────────────────────────────────────┼───────────────────┘
             │ HTTPS (tunnel / api.muns.io)          │ mirror writes
             ▼                                       │
   ┌─────────────────────  AWS EC2 (docker compose) ─┼───────────────────┐
   │                                                 │                    │
   │   ① api-gateway :8056                           │                    │
   │      /public/google-meet  ──resolve key──► admin-api :8057          │
   │      /public/join (by email)                    │                    │
   │             │ forward POST /bots                 │                    │
   │             ▼                                    │                    │
   │   ② meeting-api :8080  ── creates Meeting row (status=requested)     │
   │             │  docker run vexa-bot  (passes url, token, callbacks)   │
   │             ▼                                    │                    │
   │   ③ vexa-bot (ephemeral Chromium)                                   │
   │        • Playwright opens meet.google.com/<code>                     │
   │        • "Awaiting admission" ──► human admits ──► status=active     │
   │        • MediaRecorder → webm chunks ──upload──► meeting-api ──► MinIO│
   │        • active-speaker timeline sampled from DOM                    │
   │             │ (bot leaves: stop / alone 15m / no-join 2m / max 2h)   │
   │             ▼ callback: completed                                    │
   │   ④ meeting-api post_meeting                                         │
   │        • batch_transcribe: assemble all chunks → 1 audio blob        │
   │        • POST transcription-service /v1/transcribe/batch ──► Deepgram│
   │        • DELETE old segments → INSERT diarized segments (Postgres)   │
   │        • mirror meeting+segments ──► Cloudflare D1 (owner_email) ────┘
   │        • deliver webhook (if configured)                             │
   └──────────────────────────────────────────────────────────────────────┘
```

### The 6 stages

1. **Request** — Client → CF frontend → `api-gateway`. `/public/google-meet`
   (anonymous, system user) or `/public/join` (find-or-create user by email →
   per-client isolation). Gateway resolves the key via `admin-api`, forwards to
   `meeting-api POST /bots`.
2. **Launch** — `meeting-api` writes a `Meeting` row (`requested`), then
   `docker run`s a `vexa-bot` container with the meeting URL, a session token,
   callback URLs, and resolved timeouts.
3. **Join** — Bot boots Chromium (Playwright), opens the Meet, knocks
   ("Awaiting admission"). A human admits it → `active`.
4. **Record** — Browser `MediaRecorder` captures audio → **webm chunks** →
   uploaded to `meeting-api` → **MinIO**. An **active-speaker timeline** is
   sampled from the Meet DOM. In default **batch mode, live transcription is OFF**
   (`transcribe_enabled:false`) — nothing is transcribed mid-call.
5. **Leave** — Triggered by the stop endpoint (`DELETE /bots/...`), or timeouts
   (left-alone 15 min, no-one-joined 2 min, hard cap 2 h). Bot exits → callback
   `completed`.
6. **Transcribe & store** — `post_meeting` assembles all chunks into one audio
   file, sends it to `transcription-service` → **Deepgram `nova-3`**
   (multi-language + diarization). Segments are written **once**
   (`DELETE … WHERE meeting_id` then `INSERT`), mirrored to **Cloudflare D1**
   keyed by `owner_email`, and any webhook fires. The frontend reads transcripts
   straight from D1.

---

## 5. Why batch (not live) transcription

This fork deliberately **records the whole meeting and transcribes once at the
end** instead of per-chunk live. Reason: per-chunk live transcription produced
garbage on Hindi/English code-switching and mis-attributed speakers. Whole-audio
Deepgram with diarization fixes both language detection and speaker attribution.

**Cooperation guarantee:** with `BATCH_TRANSCRIBE_ENABLED=true` (default) +
`RECORDING_ENABLED=true`, the bot's realtime transcribe is switched **off**, so a
segment is written **exactly once** by the batch path. A manual
`POST /meetings/{id}/transcribe` endpoint exists but is dormant and 409-guarded
once segments exist. Escape hatch for live: `LIVE_TRANSCRIPTION_ENABLED=true`.

---

## 6. Auth & multi-tenancy

- **API keys** (`vxa_...`) → `admin-api` resolves to `{user_id, scopes, max_concurrent}`.
- `VEXA_REQUIRE_AUTH=true` gates everything.
- `/public/google-meet` → uses the server-side **`PUBLIC_BOT_API_KEY`** (one shared system user).
- `/public/join` → uses **`ADMIN_API_TOKEN`** to **find-or-create a user by email** →
  each client gets an isolated transcript set.
- **Concurrency caps:** `GLOBAL_MAX_CONCURRENT_BOTS=7`, `DEFAULT_MAX_CONCURRENT_BOTS=2`/user.
- `owner_email` column on the D1 mirror lets the Cloudflare frontend filter
  transcripts per client.

---

## 7. Calendar auto-join

```
client connects Google Calendar (OAuth, once)
   → calendar-service stores per-user tokens
   → sync.py polls calendar events, extracts Meet URLs
   → runtime-api scheduler creates an auto-join job per event
   → at meeting time → meeting-api launches a bot automatically
```

- OAuth scopes: `openid email calendar.readonly`.
- **No-knock**: a shared "notetaker" identity (`NOTETAKER_S3_PATH`) lets the bot
  enter without manual admission for owned calendars.
- ⚠️ The Google OAuth app is still in **Testing** mode → test-user allowlist +
  7-day token expiry. Publishing to Production (needs a stable domain) removes both.

---

## 8. Measured resource profile (for AWS sizing)

| Component | RAM | CPU |
|---|---|---|
| All 10 backend services, idle | ~840 MiB | ~0 cores |
| **Each bot, actively recording** | **1.06–1.26 GiB** | 0.5–1 core |
| 7 concurrent bots (global cap) | ~9 GiB | ~5.5 cores |

**Recommended AWS:** `m5.2xlarge` (8 vCPU / 32 GB) for the full 7-bot ceiling, or
`c5.2xlarge` (8/16 GB) to save cost. **No GPU** (transcription is cloud).
**~80–100 GB gp3 EBS** (bot image 6.27 GB + others ~4 GB + MinIO recordings).
Single EC2 + compose is the right shape at this scale.

---

## 9. Deploy shape

- **Local/dev:** `make all` (alias `up`) → full compose stack.
- **AWS prod:** single EC2 + this same compose, behind a stable domain
  (e.g. `api.muns.io`) replacing the ephemeral tunnel.
- Cloudflare hosts the customer frontend + D1; AWS exposes 3 surfaces to it:
  `/public/join`, `/calendar/oauth`, and direct D1 reads.			
