"""Meeting API configuration — environment variables and defaults."""

import os

# Required
REDIS_URL = os.environ.get("REDIS_URL")
if not REDIS_URL:
    raise ValueError("Missing required environment variable: REDIS_URL")

# Runtime API — where we delegate container operations
RUNTIME_API_URL = os.environ.get("RUNTIME_API_URL", "http://runtime-api:8000")
RUNTIME_API_TOKEN = os.environ.get("RUNTIME_API_TOKEN", "")

# Self URL — used for callback_url in container creation
MEETING_API_URL = os.environ.get("MEETING_API_URL", "http://meeting-api:8080")

# Bot image / profile
BOT_IMAGE_NAME = os.environ.get("BOT_IMAGE_NAME", "vexaai/vexa-bot:latest")

# CORS
_cors_raw = os.getenv("CORS_ORIGINS", "*").strip()
CORS_WILDCARD = _cors_raw == "*"
CORS_ORIGINS = ["*"] if CORS_WILDCARD else [
    origin.strip()
    for origin in _cors_raw.split(",")
    if origin.strip()
]

# Delayed stop timeout for fallback container shutdown
try:
    BOT_STOP_DELAY_SECONDS = max(0, int(os.getenv("BOT_STOP_DELAY_SECONDS", "90")))
except ValueError:
    BOT_STOP_DELAY_SECONDS = 90

# Global concurrency cap across ALL users — protects a fixed-capacity host
# (e.g. a single EC2 box sized for N bots) from being oversubscribed when many
# users each sit under their own per-user max_concurrent_bots limit. Counts
# active meeting bots across every user; 0 = disabled (no global cap, default).
try:
    GLOBAL_MAX_CONCURRENT_BOTS = max(0, int(os.getenv("GLOBAL_MAX_CONCURRENT_BOTS", "0")))
except ValueError:
    GLOBAL_MAX_CONCURRENT_BOTS = 0

# Transcription collector
TRANSCRIPTION_COLLECTOR_URL = os.getenv(
    "TRANSCRIPTION_COLLECTOR_URL",
    "http://transcription-collector:8000",
)

# Post-meeting hooks (comma-separated URLs)
POST_MEETING_HOOKS = [
    url.strip()
    for url in os.getenv("POST_MEETING_HOOKS", "").split(",")
    if url.strip()
]
