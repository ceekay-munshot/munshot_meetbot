"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  audioEndpointPath,
  canHaveRecording,
  probeMeetingAudio,
  recordingFilename,
  type RecordingProbe,
} from "@/lib/recording";
import type { Meeting } from "@/types/vexa";

export interface MeetingRecording {
  /** True while the availability probe is in flight. */
  isChecking: boolean;
  /** Null until the first probe resolves, or when the meeting can't have one yet. */
  probe: RecordingProbe | null;
  isAvailable: boolean;
  /** Same-origin URL to stream/download the audio from. */
  url: string;
  /** Suggested download filename. */
  filename: string;
  /** Re-run the probe — recordings can land a little after the meeting ends. */
  refresh: () => void;
}

/**
 * Resolves whether a meeting has recorded audio behind
 * GET /audio/{platform}/{native_meeting_id}.
 *
 * Shared by the player card and the export menus so a single probe answers for
 * the whole page.
 */
export function useMeetingRecording(meeting: Meeting | null): MeetingRecording {
  const platform = meeting?.platform;
  const nativeId = meeting?.platform_specific_id;
  const status = meeting?.status;
  const eligible = Boolean(platform && nativeId && status && canHaveRecording(status));

  const [reloadToken, setReloadToken] = useState(0);
  // Results are tagged with the probe they came from, so navigating to another
  // meeting (or re-probing after it ends) never shows the previous answer.
  // `status` is part of the key because the audio only lands once the bot has
  // finished uploading it.
  const probeKey = eligible ? `${platform}/${nativeId}/${status}/${reloadToken}` : "";
  const [result, setResult] = useState<{ key: string; probe: RecordingProbe } | null>(null);

  useEffect(() => {
    if (!probeKey || !platform || !nativeId) return;

    const controller = new AbortController();
    let cancelled = false;

    probeMeetingAudio(platform, nativeId, controller.signal).then((probe) => {
      if (!cancelled) setResult({ key: probeKey, probe });
    });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [probeKey, platform, nativeId]);

  const refresh = useCallback(() => setReloadToken((token) => token + 1), []);

  const probe = result && result.key === probeKey ? result.probe : null;

  const url = useMemo(
    () => (platform && nativeId ? audioEndpointPath(platform, nativeId) : ""),
    [platform, nativeId]
  );

  const filename = useMemo(
    () => (meeting ? recordingFilename(meeting, probe?.contentType) : ""),
    [meeting, probe?.contentType]
  );

  return {
    isChecking: Boolean(probeKey) && probe === null,
    probe,
    isAvailable: probe?.availability.state === "available",
    url,
    filename,
    refresh,
  };
}
