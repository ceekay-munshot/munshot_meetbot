#!/usr/bin/env python3
"""
connect_calendar.py — one-shot Google Calendar OAuth → calendar-service activator.

Mints a calendar.readonly refresh_token (offline access) using the SAME Google
OAuth client the calendar-service uses to refresh it, then posts
{email, refresh_token} to the running calendar-service /calendar/oauth endpoint.

Usage:
    python3 connect_calendar.py

It reads GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / VEXA_API_KEY from .env,
opens a Google consent URL, captures the redirect on a local loopback port,
exchanges the code, and activates the calendar.

PREREQUISITE (Google Cloud Console, one-time):
  - Google Calendar API enabled for the project
  - OAuth consent screen: scope .../auth/calendar.readonly added; the account
    you authorize added as a Test user (app in Testing mode)
  - This EXACT redirect URI added to the OAuth client's Authorized redirect URIs:
        http://localhost:8765/oauth2callback
"""

import json
import sys
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

REDIRECT_PORT = 8765
REDIRECT_PATH = "/oauth2callback"
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}{REDIRECT_PATH}"
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_SERVICE = "http://localhost:8050/calendar/oauth"


def load_env(path=".env"):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        sys.exit(f"ERROR: {path} not found. Run this from the repo root.")
    return env


_captured = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != REDIRECT_PATH:
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        _captured["code"] = qs.get("code", [None])[0]
        _captured["error"] = qs.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = "Authorization captured. You can close this tab and return to the terminal."
        if _captured["error"]:
            msg = f"OAuth error: {_captured['error']}. Return to the terminal."
        self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode())

    def log_message(self, *args):
        pass


def main():
    env = load_env()
    client_id = env.get("GOOGLE_CLIENT_ID", "")
    client_secret = env.get("GOOGLE_CLIENT_SECRET", "")
    api_key = env.get("VEXA_API_KEY", "")
    if not client_id or not client_secret:
        sys.exit("ERROR: GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET missing from .env")

    email = input("Email of the calendar to connect (the Google account you'll authorize): ").strip().lower()
    if "@" not in email:
        sys.exit("ERROR: invalid email")

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    consent_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    print("\n" + "=" * 70)
    print("STEP 1 — Open this URL in a browser and authorize with:", email)
    print("=" * 70)
    print(consent_url)
    print("=" * 70 + "\n")
    try:
        webbrowser.open(consent_url)
    except Exception:
        pass

    print(f"Waiting for the Google redirect on {REDIRECT_URI} ...")
    server = HTTPServer(("localhost", REDIRECT_PORT), Handler)
    server.handle_request()  # blocks until the one redirect arrives

    if _captured.get("error"):
        sys.exit(f"\nOAuth error: {_captured['error']}")
    code = _captured.get("code")
    if not code:
        sys.exit("\nNo authorization code received.")

    print("Got authorization code. Exchanging for refresh_token ...")
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            tok = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"Token exchange failed: {e.code} {e.read().decode()}")

    refresh_token = tok.get("refresh_token")
    if not refresh_token:
        sys.exit(
            "No refresh_token returned. This happens if the account already "
            "granted consent without prompt=consent. Revoke at "
            "https://myaccount.google.com/permissions and retry."
        )
    print("Got refresh_token:", refresh_token[:12] + "…")

    print(f"\nSTEP 2 — Activating calendar at {CALENDAR_SERVICE} ...")
    body = json.dumps({"email": email, "refresh_token": refresh_token}).encode()
    req2 = urllib.request.Request(
        CALENDAR_SERVICE, data=body, method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
    )
    try:
        with urllib.request.urlopen(req2) as r:
            result = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"calendar-service rejected: {e.code} {e.read().decode()}")

    print("\n" + "=" * 70)
    print("CALENDAR CONNECTED")
    print(json.dumps(result, indent=2))
    print("=" * 70)
    print(f"\nuser_id={result.get('user_id')} — events_synced={result.get('events_synced')}")
    print("The sync loop will now poll every 5 min and auto-join Google Meet events.")


if __name__ == "__main__":
    main()
