// Meeting recording (audio) helpers.
//
// The API exposes the recorded meeting audio at
//   GET /audio/{platform}/{native_meeting_id}   (X-API-Key auth)
// which streams the raw media bytes (webm by default). The dashboard reaches it
// through the same-origin proxy at /api/vexa/* so the browser sends the session
// cookie instead of an API key, and so <audio> can range-request it directly.
//
// A 404 carries meaning: the recording was either dropped by the retention
// policy or never captured. Those read very differently to a user, so we keep
// the distinction instead of collapsing both into "not found".

import { format } from "date-fns";
import { withBasePath } from "@/lib/base-path";
import { parseUTCTimestamp } from "@/lib/utils";
import type { Meeting, Platform } from "@/types/vexa";

export type RecordingAvailability =
  | { state: "available" }
  | { state: "unavailable"; reason: "deleted" | "never_recorded"; message: string }
  | { state: "error"; message: string };

export interface RecordingProbe {
  availability: RecordingAvailability;
  /** Total size of the recording in bytes, when the server reported it. */
  sizeBytes: number | null;
  contentType: string | null;
}

/** Same-origin URL for the meeting audio, proxied through /api/vexa. */
export function audioEndpointPath(platform: Platform | string, nativeMeetingId: string): string {
  return withBasePath(`/api/vexa/audio/${platform}/${encodeURIComponent(nativeMeetingId)}`);
}

/** Pull the error message out of a FastAPI/JSON error body, falling back to raw text. */
function errorDetail(body: string): string {
  const trimmed = body.trim();
  if (!trimmed) return "";
  try {
    const parsed = JSON.parse(trimmed) as Record<string, unknown>;
    for (const key of ["detail", "error", "message"]) {
      const value = parsed[key];
      if (typeof value === "string" && value) return value;
    }
  } catch {
    // Not JSON — the raw text is the best we have.
  }
  return trimmed.length > 200 ? `${trimmed.slice(0, 200)}…` : trimmed;
}

/**
 * Turn an audio-endpoint response into something the UI can render.
 * Pure so the 404 wording rules stay testable.
 */
export function classifyAudioResponse(status: number, body: string): RecordingAvailability {
  if (status >= 200 && status < 300) {
    return { state: "available" };
  }

  const detail = errorDetail(body);

  if (status === 404) {
    // "deleted per retention policy" vs "no recorded audio found"
    const deleted = /retention|deleted|expired/i.test(detail);
    return {
      state: "unavailable",
      reason: deleted ? "deleted" : "never_recorded",
      message: detail || (deleted ? "Recording deleted per retention policy." : "No recording available for this meeting."),
    };
  }

  if (status === 401 || status === 403) {
    return { state: "error", message: detail || "Not authorized to access this recording." };
  }

  return { state: "error", message: detail || `Could not load the recording (HTTP ${status}).` };
}

/**
 * Total byte size of the media, preferring the `Content-Range` total (set when
 * the server honoured our 1-byte range probe) over `Content-Length`.
 */
export function parseTotalBytes(contentRange: string | null, contentLength: string | null): number | null {
  if (contentRange) {
    const match = /\/\s*(\d+)\s*$/.exec(contentRange);
    if (match) {
      const total = Number(match[1]);
      if (Number.isFinite(total)) return total;
    }
  }
  if (contentLength) {
    const total = Number(contentLength);
    // A ranged response reports the length of the slice, not the whole file, so
    // only trust Content-Length when there is no Content-Range alongside it.
    if (Number.isFinite(total) && !contentRange) return total;
  }
  return null;
}

/**
 * Ask for a single byte to learn whether a recording exists without pulling the
 * whole file down. Servers that ignore `Range` answer 200 with the full body,
 * so the body is always cancelled once the headers have been read.
 */
export async function probeMeetingAudio(
  platform: Platform | string,
  nativeMeetingId: string,
  signal?: AbortSignal
): Promise<RecordingProbe> {
  let response: Response;
  try {
    response = await fetch(audioEndpointPath(platform, nativeMeetingId), {
      headers: { Range: "bytes=0-0" },
      signal,
    });
  } catch (error) {
    return {
      availability: { state: "error", message: (error as Error).message || "Network error" },
      sizeBytes: null,
      contentType: null,
    };
  }

  const contentType = response.headers.get("content-type");
  const sizeBytes = parseTotalBytes(
    response.headers.get("content-range"),
    response.headers.get("content-length")
  );

  if (response.ok) {
    // Headers are all we need — drop the bytes on the floor.
    await response.body?.cancel().catch(() => {});
    return { availability: { state: "available" }, sizeBytes, contentType };
  }

  const body = await response.text().catch(() => "");
  return { availability: classifyAudioResponse(response.status, body), sizeBytes: null, contentType };
}

const CONTENT_TYPE_EXTENSIONS: Array<[RegExp, string]> = [
  [/webm/i, "webm"],
  [/ogg|opus/i, "ogg"],
  [/mp4|m4a|aac/i, "m4a"],
  [/mpeg|mp3/i, "mp3"],
  [/wav/i, "wav"],
];

export function extensionForContentType(contentType?: string | null): string {
  if (contentType) {
    for (const [pattern, extension] of CONTENT_TYPE_EXTENSIONS) {
      if (pattern.test(contentType)) return extension;
    }
  }
  return "webm";
}

/** Mirrors generateFilename() in lib/export.ts, but for media. */
export function recordingFilename(meeting: Meeting, contentType?: string | null): string {
  const date = meeting.start_time
    ? format(parseUTCTimestamp(meeting.start_time), "yyyy-MM-dd")
    : format(new Date(), "yyyy-MM-dd");
  const id = meeting.platform_specific_id.replace(/[^a-zA-Z0-9]/g, "-");
  return `recording-${date}-${id}.${extensionForContentType(contentType)}`;
}

export function formatBytes(bytes: number | null): string {
  if (bytes === null || !Number.isFinite(bytes) || bytes <= 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

/**
 * Recordings only exist once the bot has actually captured something, so there
 * is nothing to look for while the meeting is still being set up.
 */
export function canHaveRecording(status: Meeting["status"]): boolean {
  return status === "active" || status === "stopping" || status === "completed" || status === "failed";
}
