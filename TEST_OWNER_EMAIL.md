# Testing the owner_email D1 mirror (via tunnel)

Local stack exposed through a Cloudflare quick tunnel (no account needed).
The tunnel forwards the public URL → your local api-gateway (:8056).

> **TUNNEL_URL** = `https://save-robert-monitors-plugin.trycloudflare.com`
> (quick tunnels are ephemeral — the URL changes every time cloudflared restarts)

System key (server-to-server; your secret, the client never holds it):
`vxa_bot_0uoy7REA8QUoO7XOBQPNS3vRoCc18iMASW3P0TzE`

---

## 1. Trigger a bot — the WRITE endpoint (`POST /public/join`)
One call: `{email, meeting_url}`. The transcript is owned by `email`, which is
what lands in D1's `owner_email`.

```bash
curl -s -X POST https://save-robert-monitors-plugin.trycloudflare.com/public/join \
  -H "X-API-Key: vxa_bot_0uoy7REA8QUoO7XOBQPNS3vRoCc18iMASW3P0TzE" \
  -H "Content-Type: application/json" \
  -d '{"email":"tech@muns.io","meeting_url":"https://meet.google.com/xxx-xxxx-xxx"}'
```

Use a **real, live Meet** you have open. The bot joins, gets admitted, and starts
transcribing. Speak for ~30s so segments finalize (they mirror to D1 after the
~30s immutability window).

## 2. Read it back — the READ path (Cloudflare D1)
By design there is **no AWS read endpoint** — your frontend queries D1 directly.
Two ways to verify:

**a) D1 web console (fastest — you already use it):**
```sql
SELECT owner_email, speaker, text, start_time
FROM transcriptions
WHERE owner_email = 'tech@muns.io'
ORDER BY meeting_id, start_time;
```

**b) D1 REST API (this is exactly what your CF Worker will call):**
```bash
curl -s https://api.cloudflare.com/client/v4/accounts/489675fbe898cd94904c654de83ade00/d1/database/d43cc292-3389-42ba-bd20-184ac4335360/query \
  -H "Authorization: Bearer <CF_API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"sql":"SELECT owner_email, speaker, text FROM transcriptions WHERE owner_email = ? ORDER BY start_time","params":["tech@muns.io"]}'
```

✅ **Pass** = rows come back with `owner_email = tech@muns.io`.
If `owner_email` is NULL, the meeting-api image predates the change — rebuild it.

---

## Calendar path (optional, same tunnel)
Connect a client calendar so bots auto-dispatch:
```bash
curl -s -X POST https://save-robert-monitors-plugin.trycloudflare.com/calendar/oauth \
  -H "X-API-Key: vxa_bot_0uoy7REA8QUoO7XOBQPNS3vRoCc18iMASW3P0TzE" \
  -H "Content-Type: application/json" \
  -d '{"email":"client@acme.com","refresh_token":"<google offline refresh token>"}'
```
(Note: `/calendar/oauth` lives on the calendar-service :8050. The tunnel above
points at the gateway :8056 — if you want calendar reachable too, run a second
`cloudflared tunnel --url http://localhost:8050`, or I can add a gateway route.)
