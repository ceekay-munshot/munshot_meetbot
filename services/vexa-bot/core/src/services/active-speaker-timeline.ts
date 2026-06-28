import http from "http";
import https from "https";
import { Page } from "playwright";
import { log } from "../utils";

/**
 * Active-speaker timeline sampler (post-meeting batch transcription).
 *
 * The post-meeting batch transcript runs Deepgram diarization on the whole
 * meeting audio, which yields anonymous speaker indices (0,1,2…). To turn those
 * into real names, meeting-api overlaps each diarized time range with this
 * timeline: a periodic record of which participant tiles Google Meet itself
 * marks as "speaking" (window.__vexaGetAllParticipantNames().speaking).
 *
 * This uses Google's own speaking indicator, which is INDEPENDENT of the
 * realtime audio-track → tile vote-lock (the mechanism that previously
 * mislabeled speakers whose tile never mapped). t_ms is relative to sampler
 * start ≈ recording start ≈ Deepgram's audio t=0.
 */
interface ActiveSpeakerSample {
  t_ms: number;
  speaking: string[];
}

let samples: ActiveSpeakerSample[] = [];
let timer: ReturnType<typeof setInterval> | null = null;
let startEpochMs = 0;
let botNameLower = "";

export function startActiveSpeakerSampler(page: Page, botName?: string, intervalMs = 500): void {
  if (timer) return; // already running
  samples = [];
  startEpochMs = Date.now();
  botNameLower = (botName || "").toLowerCase();
  log(`[Active-Speaker Timeline] sampler started (every ${intervalMs}ms)`);

  timer = setInterval(async () => {
    try {
      if (page.isClosed()) return;
      const speaking = await page.evaluate(() => {
        const getNames = (window as any).__vexaGetAllParticipantNames;
        if (typeof getNames !== "function") return null;
        const data = getNames() as { names: Record<string, string>; speaking: string[] };
        return (data && data.speaking) || [];
      });
      if (!speaking) return;
      const filtered = botNameLower
        ? speaking.filter((n) => {
            const l = (n || "").toLowerCase();
            return l && !(l.includes(botNameLower) || botNameLower.includes(l));
          })
        : speaking.filter((n) => !!n);
      // Only record ticks where someone is speaking — keeps the payload compact
      // over a long meeting; the mapping only votes on speaking samples anyway.
      if (filtered.length > 0) {
        samples.push({ t_ms: Date.now() - startEpochMs, speaking: filtered });
      }
    } catch {
      /* page navigating/closing — skip this tick */
    }
  }, intervalMs);
}

export function stopActiveSpeakerSampler(): void {
  if (timer) {
    clearInterval(timer);
    timer = null;
    log(`[Active-Speaker Timeline] sampler stopped (${samples.length} samples)`);
  }
}

export function getActiveSpeakerTimeline() {
  return { recording_start_epoch_ms: startEpochMs, samples };
}

/**
 * POST the collected timeline to meeting-api. Best-effort: a failure must never
 * block the bot's graceful exit.
 */
export function uploadActiveSpeakerTimeline(
  url: string,
  token: string,
  meetingId: number,
  sessionUid: string,
): Promise<void> {
  const body = Buffer.from(
    JSON.stringify({
      meeting_id: meetingId,
      session_uid: sessionUid,
      recording_start_epoch_ms: startEpochMs,
      samples,
    }),
  );

  return new Promise<void>((resolve) => {
    try {
      const u = new URL(url);
      const transport = u.protocol === "https:" ? https : http;
      const req = transport.request(
        {
          hostname: u.hostname,
          port: u.port,
          path: u.pathname,
          method: "POST",
          timeout: 15000,
          headers: {
            "Content-Type": "application/json",
            "Content-Length": body.length,
            Authorization: `Bearer ${token}`,
          },
        },
        (res) => {
          res.on("data", () => {});
          res.on("end", () => {
            log(`[Active-Speaker Timeline] uploaded ${samples.length} samples (HTTP ${res.statusCode})`);
            resolve();
          });
        },
      );
      req.on("timeout", () => { req.destroy(); resolve(); });
      req.on("error", (e) => { log(`[Active-Speaker Timeline] upload error: ${e.message}`); resolve(); });
      req.write(body);
      req.end();
    } catch (e: any) {
      log(`[Active-Speaker Timeline] upload threw: ${e?.message}`);
      resolve();
    }
  });
}
