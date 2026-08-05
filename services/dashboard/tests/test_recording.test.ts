import { describe, it, expect } from "vitest";
import {
  audioEndpointPath,
  canHaveRecording,
  classifyAudioResponse,
  extensionForContentType,
  formatBytes,
  parseTotalBytes,
  probeMeetingAudio,
  recordingFilename,
} from "@/lib/recording";
import type { Meeting } from "@/types/vexa";

const meeting: Meeting = {
  id: "42",
  platform: "google_meet",
  platform_specific_id: "abc-defg-hij",
  status: "completed",
  start_time: "2026-08-05T09:30:00Z",
  end_time: "2026-08-05T10:15:00Z",
  bot_container_id: null,
  data: {},
  created_at: "2026-08-05T09:29:00Z",
};

describe("audioEndpointPath", () => {
  it("targets the proxied /audio/{platform}/{native_meeting_id} route", () => {
    expect(audioEndpointPath("google_meet", "abc-defg-hij")).toBe(
      "/api/vexa/audio/google_meet/abc-defg-hij"
    );
  });

  it("escapes meeting ids that are not URL-safe", () => {
    expect(audioEndpointPath("zoom", "123 456/789")).toBe(
      "/api/vexa/audio/zoom/123%20456%2F789"
    );
  });
});

describe("classifyAudioResponse", () => {
  it("treats 2xx as playable", () => {
    expect(classifyAudioResponse(200, "")).toEqual({ state: "available" });
    expect(classifyAudioResponse(206, "")).toEqual({ state: "available" });
  });

  it("separates a retention-policy delete from a meeting that was never recorded", () => {
    // The API answers 404 for both; only the wording tells them apart.
    expect(
      classifyAudioResponse(404, JSON.stringify({ detail: "Recording deleted per retention policy" }))
    ).toEqual({
      state: "unavailable",
      reason: "deleted",
      message: "Recording deleted per retention policy",
    });

    expect(classifyAudioResponse(404, JSON.stringify({ detail: "No recorded audio found" }))).toEqual({
      state: "unavailable",
      reason: "never_recorded",
      message: "No recorded audio found",
    });
  });

  it("still classifies a 404 with an empty body", () => {
    const result = classifyAudioResponse(404, "");
    expect(result).toEqual({
      state: "unavailable",
      reason: "never_recorded",
      message: "No recording available for this meeting.",
    });
  });

  it("surfaces auth failures as errors, not as a missing recording", () => {
    const result = classifyAudioResponse(401, JSON.stringify({ error: "Not authenticated" }));
    expect(result).toEqual({ state: "error", message: "Not authenticated" });
  });

  it("falls back to the raw body when the error is not JSON", () => {
    expect(classifyAudioResponse(500, "upstream exploded")).toEqual({
      state: "error",
      message: "upstream exploded",
    });
  });

  it("reports the status code when there is no body at all", () => {
    expect(classifyAudioResponse(502, "")).toEqual({
      state: "error",
      message: "Could not load the recording (HTTP 502).",
    });
  });
});

describe("parseTotalBytes", () => {
  it("reads the total out of a Content-Range", () => {
    expect(parseTotalBytes("bytes 0-0/1048576", "1")).toBe(1048576);
  });

  it("ignores Content-Length when the response is a range slice", () => {
    // Content-Length is 1 here — the length of the probe slice, not the file.
    expect(parseTotalBytes("bytes 0-0/*", "1")).toBeNull();
  });

  it("uses Content-Length for a full (non-ranged) response", () => {
    expect(parseTotalBytes(null, "2048")).toBe(2048);
  });

  it("returns null when the server reports nothing usable", () => {
    expect(parseTotalBytes(null, null)).toBeNull();
    expect(parseTotalBytes(null, "not-a-number")).toBeNull();
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
    expect(recordingFilename(meeting, "audio/webm")).toBe("recording-2026-08-05-abc-defg-hij.webm");
  });

  it("keeps the extension in step with the served content type", () => {
    expect(recordingFilename(meeting, "audio/mpeg")).toBe("recording-2026-08-05-abc-defg-hij.mp3");
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
  it("only looks for audio once the bot could have captured some", () => {
    expect(canHaveRecording("completed")).toBe(true);
    expect(canHaveRecording("active")).toBe(true);
    expect(canHaveRecording("stopping")).toBe(true);
    expect(canHaveRecording("failed")).toBe(true);
    expect(canHaveRecording("requested")).toBe(false);
    expect(canHaveRecording("joining")).toBe(false);
    expect(canHaveRecording("awaiting_admission")).toBe(false);
  });
});

describe("probeMeetingAudio", () => {
  function stubFetch(response: Response | Error) {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    globalThis.fetch = (async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      if (response instanceof Error) throw response;
      return response;
    }) as unknown as typeof fetch;
    return calls;
  }

  it("range-requests a single byte and reports the full size", async () => {
    const calls = stubFetch(
      new Response("x", {
        status: 206,
        headers: {
          "content-type": "audio/webm",
          "content-range": "bytes 0-0/5242880",
          "content-length": "1",
        },
      })
    );

    const probe = await probeMeetingAudio("google_meet", "abc-defg-hij");

    expect(calls[0].url).toBe("/api/vexa/audio/google_meet/abc-defg-hij");
    expect((calls[0].init?.headers as Record<string, string>).Range).toBe("bytes=0-0");
    expect(probe.availability).toEqual({ state: "available" });
    expect(probe.sizeBytes).toBe(5242880);
    expect(probe.contentType).toBe("audio/webm");
  });

  it("classifies the 404 body", async () => {
    stubFetch(
      new Response(JSON.stringify({ detail: "Audio deleted per retention policy" }), {
        status: 404,
        headers: { "content-type": "application/json" },
      })
    );

    const probe = await probeMeetingAudio("google_meet", "abc-defg-hij");

    expect(probe.availability).toEqual({
      state: "unavailable",
      reason: "deleted",
      message: "Audio deleted per retention policy",
    });
    expect(probe.sizeBytes).toBeNull();
  });

  it("reports a network failure as an error rather than throwing", async () => {
    stubFetch(new Error("connection refused"));

    const probe = await probeMeetingAudio("google_meet", "abc-defg-hij");

    expect(probe.availability).toEqual({ state: "error", message: "connection refused" });
  });
});
