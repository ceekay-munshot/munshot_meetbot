"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  canHaveRecording,
  knownRecordingState,
  loadMeetingRecording,
  recordingFilename,
  type LoadedRecording,
  type RecordingFailure,
  type RecordingLoad,
} from "@/lib/recording";
import type { Meeting } from "@/types/vexa";

export interface MeetingRecording {
  /** False while the meeting is too early in its lifecycle to have audio. */
  isSupported: boolean;
  isLoading: boolean;
  /** Set once the audio has been fetched — playable and saveable from memory. */
  loaded: LoadedRecording | null;
  /** Why there is nothing to play, when we know. */
  failure: RecordingFailure | null;
  /** Suggested download filename. */
  filename: string;
  /** Fetch the audio. Safe to call repeatedly; a load in flight is not duplicated. */
  load: () => Promise<RecordingLoad>;
  /** Fetch if needed, then save the file. */
  download: () => Promise<RecordingLoad>;
}

/**
 * Fetches a meeting's recorded audio on demand.
 *
 * Deliberately not eager: the API assembles the audio from raw chunks on every
 * request and answers in one shot, so "is there a recording?" costs the same as
 * downloading it. What IS free — an expired recording, from
 * data.recording.audio_deleted_at — is reported without any request.
 */
export function useMeetingRecording(meeting: Meeting | null): MeetingRecording {
  const meetingId = meeting?.id;
  const status = meeting?.status;
  const isSupported = Boolean(meetingId && status && canHaveRecording(status));
  const known = useMemo(() => knownRecordingState(meeting), [meeting]);

  const [loaded, setLoaded] = useState<LoadedRecording | null>(null);
  const [failure, setFailure] = useState<RecordingFailure | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  // The blob and the in-flight request live in refs: they must survive
  // re-renders without triggering them, and must be reachable from cleanup.
  const loadedRef = useRef<LoadedRecording | null>(null);
  const inFlightRef = useRef<Promise<RecordingLoad> | null>(null);

  // Drop the blob when the page moves to another meeting, and on unmount.
  useEffect(() => {
    return () => {
      if (loadedRef.current) URL.revokeObjectURL(loadedRef.current.objectUrl);
      loadedRef.current = null;
      inFlightRef.current = null;
      setLoaded(null);
      setFailure(null);
      setIsLoading(false);
    };
  }, [meetingId]);

  const filename = useMemo(
    () => (meeting ? recordingFilename(meeting, loaded?.contentType) : ""),
    [meeting, loaded?.contentType]
  );

  const start = useCallback((): Promise<RecordingLoad> => {
    if (known) return Promise.resolve({ ok: false, failure: known });
    if (!meetingId) {
      return Promise.resolve({
        ok: false,
        failure: { state: "error", message: "Meeting is not loaded yet." },
      });
    }
    if (loadedRef.current) return Promise.resolve({ ok: true, recording: loadedRef.current });
    if (inFlightRef.current) return inFlightRef.current;

    setIsLoading(true);
    setFailure(null);
    const request = loadMeetingRecording(meetingId)
      .then((result) => {
        if (result.ok) {
          loadedRef.current = result.recording;
          setLoaded(result.recording);
        } else {
          setFailure(result.failure);
        }
        return result;
      })
      .finally(() => {
        inFlightRef.current = null;
        setIsLoading(false);
      });

    inFlightRef.current = request;
    return request;
  }, [meetingId, known]);

  const load = useCallback(() => start(), [start]);

  const download = useCallback(async () => {
    const result = await start();
    if (result.ok) {
      const link = document.createElement("a");
      link.href = result.recording.objectUrl;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    }
    return result;
  }, [start, filename]);

  return {
    isSupported,
    isLoading,
    loaded,
    failure: failure ?? known,
    filename,
    load,
    download,
  };
}
