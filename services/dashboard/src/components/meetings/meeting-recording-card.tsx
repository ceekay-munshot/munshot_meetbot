"use client";

import { AudioLines, Download, Loader2, Play, RefreshCw } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { formatBytes } from "@/lib/recording";
import type { MeetingRecording } from "@/hooks/use-meeting-recording";
import { cn } from "@/lib/utils";

interface MeetingRecordingCardProps {
  recording: MeetingRecording;
  /** Live meetings are still being captured — say so instead of "not found". */
  isLive?: boolean;
  className?: string;
}

/**
 * Plays and downloads the meeting's recorded audio, shown alongside the
 * transcript.
 *
 * The audio is assembled server-side per request, so it is fetched only when
 * asked for — the idle state is a button, not a silent probe.
 */
export function MeetingRecordingCard({ recording, isLive, className }: MeetingRecordingCardProps) {
  const { isSupported, isLoading, loaded, failure, filename, load, download } = recording;

  if (!isSupported) return null;

  // A recording the retention sweep already purged is never coming back — don't
  // offer to fetch it.
  const isGone = failure?.state === "unavailable" && failure.reason === "deleted";
  const status =
    isLive && failure?.state === "unavailable" && failure.reason === "never_recorded"
      ? "Available after the meeting ends."
      : failure?.message;

  return (
    <Card className={className}>
      <CardContent className="py-3 space-y-2">
        <div className="flex items-center gap-2">
          <AudioLines className="h-4 w-4 text-muted-foreground shrink-0" />
          <span className="text-sm font-medium">Recording</span>

          {loaded ? (
            <span className="text-xs text-muted-foreground">{formatBytes(loaded.sizeBytes)}</span>
          ) : status ? (
            <span
              className={cn(
                "text-xs truncate",
                failure?.state === "error" ? "text-destructive" : "text-muted-foreground"
              )}
            >
              {status}
            </span>
          ) : (
            <span className="text-xs text-muted-foreground hidden sm:inline">
              Assembled on request
            </span>
          )}

          <div className="flex-1" />

          {loaded && (
            <Button variant="outline" size="sm" className="h-8 gap-2" onClick={download}>
              <Download className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">Download</span>
            </Button>
          )}

          {!loaded && !isGone && (
            <div className="flex items-center gap-1">
              <Button
                variant="outline"
                size="sm"
                className="h-8 gap-2"
                onClick={load}
                disabled={isLoading}
              >
                {isLoading ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : failure ? (
                  <RefreshCw className="h-3.5 w-3.5" />
                ) : (
                  <Play className="h-3.5 w-3.5" />
                )}
                <span className="hidden sm:inline">
                  {isLoading ? "Loading…" : failure ? "Try again" : "Load recording"}
                </span>
              </Button>
              {!failure && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-8 gap-2 text-muted-foreground"
                  onClick={download}
                  disabled={isLoading}
                >
                  <Download className="h-3.5 w-3.5" />
                  <span className="hidden sm:inline">Download</span>
                </Button>
              )}
            </div>
          )}
        </div>

        {loaded && (
          <audio
            controls
            autoPlay
            src={loaded.objectUrl}
            className="w-full"
            aria-label={`Recording of this meeting (${filename})`}
          />
        )}
      </CardContent>
    </Card>
  );
}
