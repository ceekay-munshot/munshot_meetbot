# Vexa Dashboard

## Why

Users need a visual interface to launch bots into meetings, watch live transcripts with speaker attribution, manage API tokens, and review past meetings. Without the dashboard, all interaction with Vexa requires direct API calls. The dashboard provides the self-service experience for non-technical users and a convenient dev tool for API users.

## What

A Next.js web application that provides:
- Meeting management: launch bots (with optional authenticated mode toggle), see active/past meetings
- Live transcript viewer: real-time segments via WebSocket with speaker labels
- Recording playback: audio player synced with transcript segments
- Browser session management: VNC view, save/clear storage for authenticated bot credentials
- User/token management: create API keys, configure webhooks
- Admin analytics: user and meeting statistics

### Documentation
- [Dashboard UI](../../docs/ui-dashboard.mdx)

### Dependencies

- **api-gateway** -- all API calls route through the gateway
- **admin-api** (via gateway) -- user authentication and token management
- No direct database access -- fully API-driven

## How

See Quick Start and Local Development sections below.

---

Open-source web UI for [Vexa](https://github.com/Vexa-ai/vexa): join meetings, watch live transcripts, manage users/tokens, and review transcript history.

Main backend repo: [Vexa](https://github.com/Vexa-ai/vexa)

## Quick Start (Docker)

```bash
docker run --rm -p 3000:3000 \
  -e VEXA_API_URL=http://your-vexa-host:8056 \
  -e VEXA_ADMIN_API_KEY=your_admin_api_key \
  vexaai/dashboard:latest
```

> **Production:** Use immutable tags (e.g., `0.10.0-260405-0108`) instead of `:latest` for reproducible deployments.

Then open `http://localhost:3000`. (The container listens on port 3000; the `npm run dev` server uses port 3001.)

## Local Development

The dashboard lives in the Vexa monorepo at `services/dashboard/`.

```bash
git clone https://github.com/Vexa-ai/vexa.git
cd vexa/services/dashboard
npm install
cp .env.example .env.local
npm run dev
```

Local dev server runs on `http://localhost:3001`.

## Recording Playback (Post-Meeting)

The meeting detail page shows a Recording card above the transcript: **Load recording** fetches the audio and plays it inline, **Download** saves it, and the Export menu carries a `Download recording` entry. State lives in `useMeetingRecording` (`src/hooks/use-meeting-recording.ts`), so one fetch serves both the player and Save.

Backend: `GET /public/audio/{meeting_id}` on the gateway.
- Keyed by our **database meeting id**, not `(platform, native_meeting_id)` — recurring meetings reuse the same Meet code across occurrences.
- Audio is assembled from raw chunks per request and returned in one shot (no `Range`/`206`), so there is no cheap way to ask "does a recording exist?" — asking costs the same as downloading. The UI therefore loads on an explicit click, never on page load.
- A `404` means one of two things, and the wording tells them apart: audio deleted per retention policy (`AUDIO_RETENTION_DAYS`), versus nothing was ever recorded. The UI shows the API's message either way.

Auth is a two-hop trust model, mirroring `/public/join`: the gateway authorizes on the shared system key (`PUBLIC_BOT_API_KEY`) and does **no** per-user ownership check, expecting the calling BFF to know which of its users owns which meeting. The dashboard's route (`src/app/api/recordings/[meetingId]/route.ts`) is that BFF:

1. resolves the meeting with the **caller's own token** (`GET /bots/id/{id}`, which 404s for meetings that token doesn't own — the same visibility the transcript view enforces);
2. only then fetches the audio with the system key.

Skipping step 1 would hand every logged-in user the system key's reach, letting them walk meeting ids for other tenants' recordings.

Config: `VEXA_SYSTEM_API_KEY` must equal the gateway's `PUBLIC_BOT_API_KEY`. It falls back to `VEXA_API_KEY`, which is already the same value in Compose (`PUBLIC_BOT_API_KEY=${VEXA_API_KEY}`), so Compose needs no extra setting; split deployments set it explicitly.

Notes:
- Playback is not synced to transcript segments: recording is presence-gated, so audio `t=0` does not necessarily line up with the first transcript segment.
- Older recording endpoints (`/recordings/{recording_id}/media/{media_file_id}/raw`) are still proxied by the gateway but are not used by the dashboard.

## URL Proxy Pattern

All gateway requests from the browser go through Next.js server-side proxies — never directly to the gateway URL. This avoids CORS issues and keeps the gateway URL as a single env var (`VEXA_API_URL`).

| Browser path | Proxied to | Configured in |
|---|---|---|
| `/api/vexa/*` | `${VEXA_API_URL}/*` | `src/app/api/vexa/[...path]/route.ts` |
| `/api/recordings/{meeting_id}` | `${VEXA_API_URL}/public/audio/{meeting_id}` | `src/app/api/recordings/[meetingId]/route.ts` |
| `/b/*` | `${VEXA_API_URL}/b/*` | `next.config.ts` rewrites |

**Rule:** Components must use relative paths (`/b/{token}/save`, `/api/vexa/meetings`) for fetch calls. The public gateway URL (`publicApiUrl` from `/api/config`) is only for display purposes (e.g., CDP connection strings for external tools).

In Docker Compose, `VEXA_API_URL` comes from the root `.env`. In Kubernetes, it comes from a ConfigMap.

## Zoom Notes

Zoom meeting joins require additional setup in the Vexa backend (Zoom Meeting SDK + OAuth/OBF). See the Vexa repo doc: `docs/zoom-app-setup.mdx`.

## Required Configuration

| Variable | Required | Notes |
|---|---|---|
| `VEXA_API_URL` | Yes | Vexa API base URL (usually `http://localhost:8056` for local Vexa) |
| `VEXA_ADMIN_API_KEY` | Yes | Admin API key used for auth/user management |
| `VEXA_ADMIN_API_URL` | No | Optional override; defaults to `VEXA_API_URL` |

## Common Optional Configuration

| Area | Variables |
|---|---|
| Session/auth | `NEXTAUTH_URL`, `NEXTAUTH_SECRET`, `JWT_SECRET`, `NEXT_PUBLIC_APP_URL` |
| Magic-link email | `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURE`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM` |
| Google OAuth | `ENABLE_GOOGLE_AUTH`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` |
| Zoom OAuth | `ZOOM_OAUTH_CLIENT_ID`, `ZOOM_OAUTH_CLIENT_SECRET`, `ZOOM_OAUTH_REDIRECT_URI`, `ZOOM_OAUTH_STATE_SECRET` |
| AI assistant | `AI_MODEL`, `AI_API_KEY`, `AI_BASE_URL` |
| Registration policy | `ALLOW_REGISTRATIONS`, `ALLOWED_EMAIL_DOMAINS` |
| Bot defaults | `DEFAULT_BOT_NAME` |
| Hosted mode | `NEXT_PUBLIC_HOSTED_MODE` |
| Frontend/public URLs | `NEXT_PUBLIC_APP_URL`, `NEXT_PUBLIC_BASE_URL`, `NEXT_PUBLIC_TRANSCRIPT_SHARE_BASE_URL`, `NEXT_PUBLIC_WEBAPP_URL` |

See `.env.example` for a complete template.

## Compose Example

```yaml
services:
  vexa-dashboard:
    image: vexaai/dashboard:latest  # For production, use immutable tags (e.g., 0.10.0-260405-0108)
    ports:
      - "3000:3000"
    environment:
      VEXA_API_URL: http://vexa:8056
      VEXA_ADMIN_API_KEY: ${VEXA_ADMIN_API_KEY}
```

## Test

```bash
cd services/dashboard
npm install
npm test
```

Runs vitest against `tests/` — covers `parseMeetingInput`, `parseUTCTimestamp`, `cn`, and language utilities.

## Troubleshooting

- Login or admin routes fail: verify `VEXA_ADMIN_API_KEY` is valid.
- Dashboard loads but data is empty: verify `VEXA_API_URL` is reachable from the container/runtime.
- OAuth callbacks fail: verify `NEXTAUTH_URL` and provider redirect URIs match exactly.
- Transcript page shows no data: verify the dashboard's `VEXA_API_KEY` (or user's token) has `tx` scope. Tokens with only `bot` scope can create bots but can't read transcripts (403 on `/transcripts` endpoints).
- Browser session creation fails: verify the token has `browser` scope and concurrent bot limit is not exceeded (error: "Concurrent bot limit reached (N/5)").
- POST /bots returns 422 `[object Object]`: the gateway requires `platform` + `native_meeting_id` fields. The dashboard client-side parses meeting URLs to extract these. If the URL parse fails, the raw URL is sent and the gateway rejects it.

### Verified 2026-04-05 (compose mode)

All backend calls validated from inside the dashboard container:
- GET: gateway root → 200, /meetings → 401 (correct — needs auth), /bots/status → 401, admin /users → 200, agent-api health → 200, internal auth → 200
- POST: /api/vexa/bots (browser_session) → 201, /api/vexa/bots (meeting join) → 201
- Dashboard serving on port 3001 (dev) / 3000 (production Docker)

## Screenshots

![Dashboard](docs/screenshots/01-dashboard.png)
![Join Meeting](docs/screenshots/02-join-meeting.png)
![Live Transcript](docs/screenshots/06-live-transcript.png)

## Related

- [Vexa deployment guide](https://github.com/Vexa-ai/vexa/blob/main/docs/deployment.mdx)
- [Vexa Lite deployment guide](https://github.com/Vexa-ai/vexa/blob/main/docs/vexa-lite-deployment.mdx)
- [Vexa API guide](https://github.com/Vexa-ai/vexa/blob/main/docs/user_api_guide.mdx)

## DoD

| # | Check | Weight | Ceiling | Status | Evidence | Last checked | Tests |
|---|-------|--------|---------|--------|----------|--------------|-------|
| 1 | Dashboard loads on port 3000 (Docker) / 3001 (dev) without errors | 20 | ceiling | untested | — | — | — |
| 2 | `VEXA_API_URL` reachable and proxy `/api/vexa/*` returns upstream responses | 25 | ceiling | untested | — | — | — |
| 3 | Login flow works (admin key or OAuth) and user session established | 20 | ceiling | untested | — | — | — |
| 4 | Meeting list page loads via `GET /meetings` through gateway | 15 | — | untested | — | — | — |
| 5 | `VEXA_ADMIN_API_KEY` set and admin endpoints accessible | 10 | ceiling | untested | — | — | — |
| 6 | `npm test` passes (parseMeetingInput, parseUTCTimestamp, cn, language utils) | 10 | — | untested | — | — | — |

Confidence: 75 (5/6 items covered by tests3: DASHBOARD_UP health check, DASHBOARD_LOGIN contract, dashboard-auth.sh (login+cookie+identity), dashboard-proxy.sh (meetings+pagination+transcript), DASHBOARD_ADMIN_KEY_VALID. -15: npm test not run. -10: DoD items not scored individually against evidence.)

## License

Apache-2.0 (`LICENSE`)
