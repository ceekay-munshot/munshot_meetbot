// Meeting recording (audio) helpers.
//
// The gateway serves recorded audio at GET /public/audio/{meeting_id} — keyed by
// our own database id, not (platform, native_meeting_id), because recurring
// meetings reuse the same native id across occurrences. It is a system-key
// endpoint, so the browser goes through the dashboard's own route at
// /api/recordings/{meeting_id}, which checks ownership before attaching that key.
//
// Two things shape the UI here:
//   * The audio is assembled from raw chunks per request and served in one shot
//     (no Range support), so there is no cheap "does it exist" probe — asking is
//     as expensive as downloading. Loading is therefore an explicit user action.
//   * A 404 carries meaning: audio dropped by the retention policy reads very
//     differently from a meeting that never recorded anything.

import { format } from "date-fns";
import { withBasePath } from "@/lib/base-path";
import { parseUTCTimestamp } from "@/lib/utils";
import type { Meeting } from "@/types/vexa";

export type RecordingFailure =
  | { state: "unavailable"; reason: "deleted" | "never_recorded"; message: string }
  | { state: "error"; message: string };

/** Same-origin URL for the meeting audio. */
export function recordingEndpointPath(meetingId: string | number): string {
  return withBasePath(`/api/recordings/${encodeURIComponent(String(meetingId))}`);
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
 * Turn a failed audio response into something the UI can render.
 * Pure so the 404 wording rules stay testable.
 */
export function classifyRecordingFailure(status: number, body: string): RecordingFailure {
  const detail = errorDetail(body);

  if (status === 404) {
    // meeting-api answers 404 three ways: "Audio ... deleted per retention
    // policy at <ts>", "No recorded audio found for this meeting", and
    // "Meeting not found: <id>". Only the first is a recording that once existed.
    const deleted = /retention|deleted|expired/i.test(detail);
    return {
      state: "unavailable",
      reason: deleted ? "deleted" : "never_recorded",
      message:
        detail ||
        (deleted
          ? "Audio for this meeting was deleted per retention policy."
          : "No recorded audio found for this meeting."),
    };
  }

  if (status === 401 || status === 403) {
    return { state: "error", message: detail || "Not authorized to access this recording." };
  }

  if (status === 504) {
    return {
      state: "error",
      message: detail || "Timed out assembling the recording. Try again in a moment.",
    };
  }

  return { state: "error", message: detail || `Could not load the recording (HTTP ${status}).` };
}

export interface LoadedRecording {
  /** Object URL for the downloaded blob — playable and saveable without refetching. */
  objectUrl: string;
  sizeBytes: number;
  contentType: string;
}

export type RecordingLoad = { ok: true; recording: LoadedRecording } | { ok: false; failure: RecordingFailure };

/**
 * Download the recording in full. There is no partial/HEAD mode upstream, and
 * the same bytes back both playback and Save, so one fetch does both jobs.
 */
export async function loadMeetingRecording(
  meetingId: string | number,
  signal?: AbortSignal
): Promise<RecordingLoad> {
  let response: Response;
  try {
    response = await fetch(recordingEndpointPath(meetingId), { signal });
  } catch (error) {
    return { ok: false, failure: { state: "error", message: (error as Error).message || "Network error" } };
  }

  if (!response.ok) {
    const body = await response.text().catch(() => "");
    return { ok: false, failure: classifyRecordingFailure(response.status, body) };
  }

  const blob = await response.blob();
  return {
    ok: true,
    recording: {
      objectUrl: URL.createObjectURL(blob),
      sizeBytes: blob.size,
      contentType: blob.type || response.headers.get("content-type") || "",
    },
  };
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
 * What we can say about a recording without spending a request.
 *
 * The retention sweep stamps `data.recording.audio_deleted_at` when it purges
 * the chunks, so an expired recording is knowable for free. Absence of
 * `data.recording` is NOT proof there is no audio — that key is only written
 * when a chunk arrives flagged final — so it stays "unknown" and the user
 * decides whether to ask.
 */
export function knownRecordingState(meeting: Meeting | null): RecordingFailure | null {
  const deletedAt = meeting?.data?.recording?.audio_deleted_at;
  if (typeof deletedAt === "string" && deletedAt) {
    return {
      state: "unavailable",
      reason: "deleted",
      message: `Audio was deleted per retention policy on ${formatDeletedAt(deletedAt)}.`,
    };
  }
  return null;
}

function formatDeletedAt(timestamp: string): string {
  try {
    return format(parseUTCTimestamp(timestamp), "d MMM yyyy");
  } catch {
    return timestamp;
  }
}

/**
 * Recordings only exist once the bot has actually captured something, so there
 * is nothing to offer while the meeting is still being set up.
 */
export function canHaveRecording(status: Meeting["status"]): boolean {
  return status === "active" || status === "stopping" || status === "completed" || status === "failed";
}
