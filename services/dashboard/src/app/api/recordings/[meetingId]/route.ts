// Meeting recording download.
//
// The gateway serves recorded audio at GET /public/audio/{meeting_id}, which is
// a server-to-server endpoint: it authorizes on the shared system key alone and
// does NO per-user ownership check (the calling BFF is expected to know which of
// its users owns which meeting). So this route does two hops:
//
//   1. resolve the meeting with the *caller's own* token — /bots/id/{id} 404s
//      for meetings that token doesn't own, which is exactly the visibility the
//      transcript view already enforces;
//   2. only then fetch the audio with the system key.
//
// Skipping step 1 would hand every logged-in user the system key's reach: they
// could walk meeting ids and pull other tenants' recordings.

import { NextRequest, NextResponse } from "next/server";
import { cookies } from "next/headers";
import { getAuthCookieName } from "@/lib/auth-cookies";

export const dynamic = "force-dynamic";
export const revalidate = 0;

// Audio is assembled from raw chunks per request, so a long meeting can take a
// while to answer — well past the 30s the generic /api/vexa proxy allows.
const AUDIO_TIMEOUT_MS = 120_000;

function jsonError(detail: string, status: number) {
  return NextResponse.json({ detail }, { status, headers: { "Cache-Control": "no-store" } });
}

export async function GET(
  request: NextRequest,
  context: { params: Promise<{ meetingId: string }> }
): Promise<NextResponse> {
  const VEXA_API_URL = process.env.VEXA_API_URL;
  if (!VEXA_API_URL) {
    return jsonError("VEXA_API_URL is required; dashboard API proxy has no API SSOT", 500);
  }

  const { meetingId } = await context.params;
  // The upstream route is /public/audio/{int}; reject anything else here rather
  // than forwarding a path that can only 422.
  if (!/^\d+$/.test(meetingId)) {
    return jsonError("Invalid meeting id", 400);
  }

  const cookieStore = await cookies();
  const userToken = cookieStore.get(getAuthCookieName())?.value;
  const requireAuth = ["1", "true", "yes"].includes(
    (process.env.VEXA_REQUIRE_AUTH || "").toLowerCase()
  );
  if (requireAuth && !userToken) {
    return jsonError("Not authenticated", 401);
  }
  const callerKey = userToken || (requireAuth ? "" : process.env.VEXA_API_KEY || "");
  if (!callerKey) {
    return jsonError("Not authenticated", 401);
  }

  // Hop 1 — ownership.
  let ownerResponse: Response;
  try {
    ownerResponse = await fetch(`${VEXA_API_URL}/bots/id/${meetingId}`, {
      headers: { "X-API-Key": callerKey },
      cache: "no-store",
      signal: AbortSignal.timeout(15_000),
    });
  } catch (error) {
    return jsonError(`Failed to connect to API: ${(error as Error).message}`, 502);
  }
  if (!ownerResponse.ok) {
    return jsonError(
      ownerResponse.status === 404
        ? "Meeting not found"
        : "Not authorized to access this meeting",
      ownerResponse.status === 404 ? 404 : 403
    );
  }

  // Hop 2 — the audio itself, with the system key. In compose this is the same
  // value the gateway holds as PUBLIC_BOT_API_KEY, hence the VEXA_API_KEY
  // fallback; deployments that split them set VEXA_SYSTEM_API_KEY explicitly.
  const systemKey = process.env.VEXA_SYSTEM_API_KEY || process.env.VEXA_API_KEY || "";
  if (!systemKey) {
    return jsonError(
      "Recording download is not configured (set VEXA_SYSTEM_API_KEY to the gateway's PUBLIC_BOT_API_KEY)",
      503
    );
  }

  let audioResponse: Response;
  try {
    audioResponse = await fetch(`${VEXA_API_URL}/public/audio/${meetingId}`, {
      headers: { "X-API-Key": systemKey },
      cache: "no-store",
      signal: AbortSignal.timeout(AUDIO_TIMEOUT_MS),
    });
  } catch (error) {
    const err = error as Error;
    if (err.name === "TimeoutError" || err.name === "AbortError") {
      return jsonError("Timed out assembling the recording", 504);
    }
    return jsonError(`Failed to connect to API: ${err.message}`, 502);
  }

  if (!audioResponse.ok) {
    const body = await audioResponse.text().catch(() => "");
    // A rejected system key is a dashboard misconfiguration, not a stale user
    // session — don't let it read as "log in again".
    if (audioResponse.status === 401 || audioResponse.status === 403) {
      return jsonError(
        "The recording service rejected the dashboard's system key (check VEXA_SYSTEM_API_KEY against the gateway's PUBLIC_BOT_API_KEY)",
        502
      );
    }
    try {
      return NextResponse.json(JSON.parse(body), {
        status: audioResponse.status,
        headers: { "Cache-Control": "no-store" },
      });
    } catch {
      return jsonError(body || `Could not load the recording (HTTP ${audioResponse.status})`, audioResponse.status);
    }
  }

  const headers = new Headers({ "Cache-Control": "no-store" });
  for (const header of ["content-type", "content-length", "content-disposition"]) {
    const value = audioResponse.headers.get(header);
    if (value) headers.set(header, value);
  }
  return new NextResponse(audioResponse.body, { status: 200, headers });
}
