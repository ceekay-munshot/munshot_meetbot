import { describe, it, expect } from "vitest";
import {
  canHaveRecording,
  classifyRecordingFailure,
  extensionForContentType,
  formatBytes,
  knownRecordingState,
  loadMeetingRecording,
  recordingEndpointPath,
  recordingFilename,
} from "@/lib/recording";
import type { Meeting } from "@/types/vexa";

const meeting: Meeting = {
  id: "126",
  platform: "google_meet",
  platform_specific_id: "toq-xnph-evg",
  status: "completed",
  start_time: "2026-08-05T09:30:00Z",
  end_time: "2026-08-05T10:15:00Z",
  bot_container_id: null,
  data: {},
  created_at: "2026-08-05T09:29:00Z",
};

describe("recordingEndpointPath", () => {
  it("targets the dashboard's own recording route, keyed by internal meeting id", () => {
    // Keyed by our database id, not (platform, native_meeting_id) — recurring
    // meetings reuse the same Meet code across every occurrence.
    expect(recordingEndpointPath(126)).toBe("/api/recordings/126");
    expect(recordingEndpointPath("126")).toBe("/api/recordings/126");
  });
});

describe("classifyRecordingFailure", () => {
  it("separates a retention-policy delete from a meeting that was never recorded", () => {
    // meeting-api answers 404 for both; only the wording tells them apart.
    expect(
      classifyRecordingFailure(
        404,
        JSON.stringify({ detail: "Audio for this meeting was deleted per retention policy at 2026-08-01T00:00:00Z" })
      )
    ).toEqual({
      state: "unavailable",
      reason: "deleted",
      message: "Audio for this meeting was deleted per retention policy at 2026-08-01T00:00:00Z",
    });

    expect(
      classifyRecordingFailure(404, JSON.stringify({ detail: "No recorded audio found for this meeting" }))
    ).toEqual({
      state: "unavailable",
      reason: "never_recorded",
      message: "No recorded audio found for this meeting",
    });
  });

  it("treats an unknown meeting id as nothing recorded, not as an error", () => {
    const result = classifyRecordingFailure(404, JSON.stringify({ detail: "Meeting not found: 999" }));
    expect(result).toEqual({
      state: "unavailable",
      reason: "never_recorded",
      message: "Meeting not found: 999",
    });
  });

  it("still classifies a 404 with an empty body", () => {
    expect(classifyRecordingFailure(404, "")).toEqual({
      state: "unavailable",
      reason: "never_recorded",
      message: "No recorded audio found for this meeting.",
    });
  });

  it("surfaces auth failures as errors, not as a missing recording", () => {
    expect(classifyRecordingFailure(403, JSON.stringify({ detail: "Not authorized to access this meeting" }))).toEqual({
      state: "error",
      message: "Not authorized to access this meeting",
    });
  });

  it("explains a timeout — assembly is slow for long meetings", () => {
    expect(classifyRecordingFailure(504, JSON.stringify({ detail: "Timed out assembling the recording" }))).toEqual({
      state: "error",
      message: "Timed out assembling the recording",
    });
    expect(classifyRecordingFailure(504, "")).toEqual({
      state: "error",
      message: "Timed out assembling the recording. Try again in a moment.",
    });
  });

  it("falls back to the raw body when the error is not JSON", () => {
    expect(classifyRecordingFailure(500, "upstream exploded")).toEqual({
      state: "error",
      message: "upstream exploded",
    });
  });

  it("reports the status code when there is no body at all", () => {
    expect(classifyRecordingFailure(502, "")).toEqual({
      state: "error",
      message: "Could not load the recording (HTTP 502).",
    });
  });
});

describe("knownRecordingState", () => {
  it("reports an expired recording from meeting data, with no request", () => {
    const expired: Meeting = {
      ...meeting,
      data: { recording: { session_uid: "abc", audio_deleted_at: "2026-08-01T03:00:00Z" } },
    };
    expect(knownRecordingState(expired)).toEqual({
      state: "unavailable",
      reason: "deleted",
      message: "Audio was deleted per retention policy on 1 Aug 2026.",
    });
  });

  it("stays silent when nothing is known — absent recording data does not prove absent audio", () => {
    // data.recording is only written when a chunk arrives flagged final, so its
    // absence is not evidence either way.
    expect(knownRecordingState(meeting)).toBeNull();
    expect(knownRecordingState({ ...meeting, data: { recording: { session_uid: "abc" } } })).toBeNull();
    expect(knownRecordingState(null)).toBeNull();
  });
});

describe("extensionForContentType", () => {
  it("maps the media types the bot can emit", () => {
    expect(extensionForContentType("audio/webm;codecs=opus")).toBe("webm");
    expect(extensionForContentType("audio/ogg")).toBe("ogg");
    expect(extensionForContentType("audio/mp4")).toBe("m4a");
    expect(extensionForContentType("audio/mpeg")).toBe("mp3");
    expect(extensionForContentType("audio/x-wav")).toBe("wav");
  });

  it("defaults to webm when the server does not say", () => {
    expect(extensionForContentType(null)).toBe("webm");
    expect(extensionForContentType("application/octet-stream")).toBe("webm");
  });
});

describe("recordingFilename", () => {
  it("names the file after the meeting date and id", () => {
    expect(recordingFilename(meeting, "audio/webm")).toBe("recording-2026-08-05-toq-xnph-evg.webm");
  });

  it("keeps the extension in step with the served content type", () => {
    expect(recordingFilename(meeting, "audio/mpeg")).toBe("recording-2026-08-05-toq-xnph-evg.mp3");
  });
});

describe("formatBytes", () => {
  it("renders human sizes", () => {
    expect(formatBytes(900)).toBe("900 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(12 * 1024 * 1024)).toBe("12 MB");
  });

  it("renders nothing when the size is unknown", () => {
    expect(formatBytes(null)).toBe("");
    expect(formatBytes(0)).toBe("");
  });
});

describe("canHaveRecording", () => {
  it("only offers audio once the bot could have captured some", () => {
    expect(canHaveRecording("completed")).toBe(true);
    expect(canHaveRecording("active")).toBe(true);
    expect(canHaveRecording("stopping")).toBe(true);
    expect(canHaveRecording("failed")).toBe(true);
    expect(canHaveRecording("requested")).toBe(false);
    expect(canHaveRecording("joining")).toBe(false);
    expect(canHaveRecording("awaiting_admission")).toBe(false);
  });
});

describe("loadMeetingRecording", () => {
  function stubFetch(response: Response | Error) {
    const calls: string[] = [];
    globalThis.fetch = (async (url: string) => {
      calls.push(url);
      if (response instanceof Error) throw response;
      return response;
    }) as unknown as typeof fetch;
    return calls;
  }

  const objectUrls: string[] = [];
  globalThis.URL.createObjectURL = ((blob: Blob) => {
    objectUrls.push(blob.type);
    return `blob:mock/${objectUrls.length}`;
  }) as typeof URL.createObjectURL;

  it("returns a playable object URL plus the real byte size", async () => {
    const calls = stubFetch(
      new Response(new Blob([new Uint8Array(2048)], { type: "audio/webm" }), {
        status: 200,
        headers: { "content-type": "audio/webm" },
      })
    );

    const result = await loadMeetingRecording(126);

    expect(calls[0]).toBe("/api/recordings/126");
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.recording.objectUrl).toMatch(/^blob:mock\//);
      expect(result.recording.sizeBytes).toBe(2048);
      expect(result.recording.contentType).toBe("audio/webm");
    }
  });

  it("classifies the failure body instead of throwing", async () => {
    stubFetch(
      new Response(JSON.stringify({ detail: "No recorded audio found for this meeting" }), {
        status: 404,
        headers: { "content-type": "application/json" },
      })
    );

    const result = await loadMeetingRecording(126);

    expect(result).toEqual({
      ok: false,
      failure: {
        state: "unavailable",
        reason: "never_recorded",
        message: "No recorded audio found for this meeting",
      },
    });
  });

  it("reports a network failure as an error", async () => {
    stubFetch(new Error("connection refused"));

    const result = await loadMeetingRecording(126);

    expect(result).toEqual({ ok: false, failure: { state: "error", message: "connection refused" } });
  });
});
