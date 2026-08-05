"use client";

import { AudioLines, Download, Loader2, RefreshCw } from "lucide-react";
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
 * transcript. Renders nothing until the availability probe has answered so a
 * completed meeting never flashes "no recording" before we know.
 */
export function MeetingRecordingCard({ recording, isLive, className }: MeetingRecordingCardProps) {
  const { isChecking, probe, url, filename, refresh } = recording;

  if (isChecking && !probe) {
    return (
      <Card className={className}>
        <CardContent className="flex items-center gap-2 py-3 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Checking for a recording…
        </CardContent>
      </Card>
    );
  }

  if (!probe) return null;

  const { availability } = probe;

  if (availability.state === "available") {
    const size = formatBytes(probe.sizeBytes);
    return (
      <Card className={className}>
        <CardContent className="py-3 space-y-2">
          <div className="flex items-center gap-2">
            <AudioLines className="h-4 w-4 text-muted-foreground shrink-0" />
            <span className="text-sm font-medium">Recording</span>
            {size && <span className="text-xs text-muted-foreground">{size}</span>}
            <div className="flex-1" />
            <Button variant="outline" size="sm" className="h-8 gap-2" asChild>
              {/* Same-origin download — the proxy streams the bytes, so the
                  browser never has to buffer the whole file in memory. */}
              <a href={url} download={filename}>
                <Download className="h-3.5 w-3.5" />
                <span className="hidden sm:inline">Download</span>
              </a>
            </Button>
          </div>
          <audio
            controls
            preload="metadata"
            src={url}
            className="w-full"
            aria-label="Meeting recording"
          />
        </CardContent>
      </Card>
    );
  }

  const message =
    availability.state === "error"
      ? availability.message
      : isLive && availability.reason === "never_recorded"
        ? "The recording becomes available after the meeting ends."
        : availability.message;

  return (
    <Card className={className}>
      <CardContent className="flex items-center gap-2 py-3">
        <AudioLines className="h-4 w-4 text-muted-foreground shrink-0" />
        <span className="text-sm font-medium">Recording</span>
        <span
          className={cn(
            "text-xs truncate",
            availability.state === "error" ? "text-destructive" : "text-muted-foreground"
          )}
        >
          {message}
        </span>
        <div className="flex-1" />
        {/* Recordings can land a little after the meeting ends. */}
        <Button variant="ghost" size="sm" className="h-8 gap-2 text-muted-foreground" onClick={refresh}>
          <RefreshCw className="h-3.5 w-3.5" />
          <span className="hidden sm:inline">Check again</span>
        </Button>
      </CardContent>
    </Card>
  );
}
