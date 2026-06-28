"""
Cloud STT provider routing for the Vexa transcription service.

When TRANSCRIPTION_PROVIDER is set to a cloud value (e.g. "cloud"/"groq"),
the service stops running faster-whisper locally and instead forwards each
audio chunk to a hosted speech-to-text provider, normalizing the response
back into the OpenAI-Whisper-compatible shape Vexa expects:

    {text, language, language_probability, duration, segments:[{start,end,text,...}]}

Provider chain (real-time, per-chunk):
    1. Groq Whisper  (primary, free tier) - OpenAI-compatible /v1/audio/transcriptions
    2. Deepgram      (fallback, paid)     - /v1/listen with the chunk bytes in the body

Groq is tried first. On any Groq failure (rate limit 429, 5xx, timeout, network
error, bad/missing key) we fall back to Deepgram. Speaker attribution is left to
Vexa's own speaker mapping, so Deepgram diarization is intentionally NOT requested.
"""
import os
import math
import random
import asyncio
import logging
from typing import Optional, List, Dict, Any

import httpx

logger = logging.getLogger(__name__)


# Providers report the spoken language inconsistently: Groq's Whisper verbose_json
# returns the full English name ("English"), while Deepgram returns a BCP-47 tag
# that may carry a region suffix ("en-US"). Downstream (meeting-api) only accepts
# bare faster-whisper ISO codes ("en"), so normalize to that here at the boundary.
_LANG_NAME_TO_CODE = {
    "english": "en", "chinese": "zh", "mandarin": "zh", "cantonese": "yue",
    "german": "de", "spanish": "es", "russian": "ru", "korean": "ko",
    "french": "fr", "japanese": "ja", "portuguese": "pt", "turkish": "tr",
    "polish": "pl", "catalan": "ca", "dutch": "nl", "arabic": "ar",
    "swedish": "sv", "italian": "it", "indonesian": "id", "hindi": "hi",
    "finnish": "fi", "vietnamese": "vi", "hebrew": "he", "ukrainian": "uk",
    "greek": "el", "malay": "ms", "czech": "cs", "romanian": "ro",
    "danish": "da", "hungarian": "hu", "tamil": "ta", "norwegian": "no",
    "nynorsk": "nn", "thai": "th", "urdu": "ur", "croatian": "hr",
    "bulgarian": "bg", "lithuanian": "lt", "latin": "la", "maori": "mi",
    "malayalam": "ml", "welsh": "cy", "slovak": "sk", "telugu": "te",
    "persian": "fa", "latvian": "lv", "bengali": "bn", "serbian": "sr",
    "azerbaijani": "az", "slovenian": "sl", "kannada": "kn", "estonian": "et",
    "macedonian": "mk", "breton": "br", "basque": "eu", "icelandic": "is",
    "armenian": "hy", "nepali": "ne", "mongolian": "mn", "bosnian": "bs",
    "kazakh": "kk", "albanian": "sq", "swahili": "sw", "galician": "gl",
    "marathi": "mr", "punjabi": "pa", "sinhala": "si", "khmer": "km",
    "shona": "sn", "yoruba": "yo", "somali": "so", "afrikaans": "af",
    "occitan": "oc", "georgian": "ka", "belarusian": "be", "tajik": "tg",
    "sindhi": "sd", "gujarati": "gu", "amharic": "am", "yiddish": "yi",
    "lao": "lo", "uzbek": "uz", "faroese": "fo", "haitian creole": "ht",
    "pashto": "ps", "turkmen": "tk", "maltese": "mt", "sanskrit": "sa",
    "luxembourgish": "lb", "myanmar": "my", "burmese": "my", "tibetan": "bo",
    "tagalog": "tl", "malagasy": "mg", "assamese": "as", "tatar": "tt",
    "hawaiian": "haw", "lingala": "ln", "hausa": "ha", "bashkir": "ba",
    "javanese": "jw", "sundanese": "su",
}


def _to_iso_language(value: Optional[str]) -> Optional[str]:
    """Coerce a provider language label to a bare faster-whisper ISO code.

    Handles full English names ("English" -> "en"), region-suffixed BCP-47 tags
    ("en-US" -> "en"), and passes through values it can't map (e.g. "unknown")
    unchanged so callers keep whatever fallback they computed.
    """
    if not value:
        return value
    v = value.strip().lower()
    if v in _LANG_NAME_TO_CODE:
        return _LANG_NAME_TO_CODE[v]
    # Strip region/script subtags: "en-US" / "zh-Hans" -> "en" / "zh".
    base = v.split("-", 1)[0]
    return base or value


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"Invalid float env {name}={raw!r}, using default {default}")
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"Invalid int env {name}={raw!r}, using default {default}")
        return default


# --- Provider selection ------------------------------------------------------
# "local" / "whisper" / "faster-whisper" (or unset) -> run faster-whisper locally.
# "cloud" / "groq" / "deepgram" / "remote"          -> forward to cloud providers.
TRANSCRIPTION_PROVIDER = _env("TRANSCRIPTION_PROVIDER", "local").lower()
_CLOUD_VALUES = {"cloud", "groq", "deepgram", "remote"}


def cloud_enabled() -> bool:
    """True when the service should forward to cloud STT providers."""
    return TRANSCRIPTION_PROVIDER in _CLOUD_VALUES


# --- Groq --------------------------------------------------------------------
GROQ_API_KEY = _env("GROQ_API_KEY")
GROQ_API_URL = _env(
    "GROQ_API_URL", "https://api.groq.com/openai/v1/audio/transcriptions"
)
GROQ_MODEL = _env("GROQ_MODEL", "whisper-large-v3-turbo")

# --- Deepgram ----------------------------------------------------------------
DEEPGRAM_API_KEY = _env("DEEPGRAM_API_KEY")
DEEPGRAM_API_URL = _env("DEEPGRAM_API_URL", "https://api.deepgram.com/v1/listen")
DEEPGRAM_MODEL = _env("DEEPGRAM_MODEL", "nova-3")

# --- Batch (post-meeting, whole-file diarized) -------------------------------
# The realtime per-chunk path auto-detects language per chunk, which flip-flops
# on Hindi/English code-switching and shreds transcription quality. The batch
# path instead sends the WHOLE meeting audio once with Deepgram nova-3's "multi"
# language (true multilingual code-switching in a single pass) + speaker
# diarization. Used by meeting-api's post-meeting job, never by realtime.
DEEPGRAM_BATCH_LANGUAGE = _env("DEEPGRAM_BATCH_LANGUAGE", "multi")
# Whole-meeting audio can be tens of MB and minutes to process; far longer than
# the realtime per-chunk timeout.
DEEPGRAM_BATCH_TIMEOUT_SECONDS = _env_float("DEEPGRAM_BATCH_TIMEOUT_SECONDS", 600.0)

# --- Shared ------------------------------------------------------------------
PROVIDER_TIMEOUT_SECONDS = _env_float("PROVIDER_TIMEOUT_SECONDS", 30.0)

# Transient failures (network blip, timeout, rate-limit 429, 5xx) are retried
# in-process with exponential backoff so a brief hiccup never bubbles up as a
# 502 / dropped chunk mid-meeting. Deterministic errors (4xx other than 429)
# are NOT retried — they'd just fail identically. Kept small so real-time
# chunks don't stack latency under a sustained outage (upstream retries too).
PROVIDER_MAX_RETRIES = _env_int("PROVIDER_MAX_RETRIES", 2)
PROVIDER_RETRY_BACKOFF_SECONDS = _env_float("PROVIDER_RETRY_BACKOFF_SECONDS", 0.5)

# Groq's Whisper API rejects any prompt longer than 896 characters with an
# HTTP 400 (deterministic — every retry fails identically). Whisper uses the
# prompt as trailing context, so we keep the tail (most recent words).
GROQ_MAX_PROMPT_CHARS = _env_int("GROQ_MAX_PROMPT_CHARS", 896)

# When true, use Groq only and skip the Deepgram fallback entirely. Groq errors
# surface directly instead of being swallowed in favour of the fallback. Handy
# for testing the Groq path in isolation.
GROQ_ONLY = _env("GROQ_ONLY", "false").lower() in {"1", "true", "yes", "on"}

# When true, use Deepgram only and skip Groq entirely (even if GROQ_API_KEY is
# set in the environment). Deepgram errors surface directly. Mirror of GROQ_ONLY
# for testing the Deepgram path in isolation.
DEEPGRAM_ONLY = _env("DEEPGRAM_ONLY", "false").lower() in {"1", "true", "yes", "on"}


class AllProvidersFailed(Exception):
    """Raised when every configured cloud provider failed for a chunk."""


def provider_status() -> Dict[str, Any]:
    """Health/diagnostics summary (never exposes the key values themselves)."""
    return {
        "mode": "cloud" if cloud_enabled() else "local",
        "provider_setting": TRANSCRIPTION_PROVIDER,
        "groq": {"configured": bool(GROQ_API_KEY), "model": GROQ_MODEL},
        "deepgram": {"configured": bool(DEEPGRAM_API_KEY), "model": DEEPGRAM_MODEL},
        "timeout_seconds": PROVIDER_TIMEOUT_SECONDS,
    }


# --- Normalization helpers ---------------------------------------------------
def _normalize_groq(data: Dict[str, Any], fallback_language: Optional[str]) -> Dict[str, Any]:
    """Groq returns the OpenAI verbose_json shape already; coerce/fill fields."""
    raw_segments = data.get("segments") or []
    segments: List[Dict[str, Any]] = []
    for idx, s in enumerate(raw_segments):
        segments.append({
            "id": s.get("id", idx),
            "seek": s.get("seek", 0),
            "start": float(s.get("start", 0.0) or 0.0),
            "end": float(s.get("end", 0.0) or 0.0),
            "text": s.get("text", "") or "",
            "tokens": s.get("tokens", []),
            "temperature": s.get("temperature", 0.0),
            "avg_logprob": s.get("avg_logprob", 0.0),
            "compression_ratio": s.get("compression_ratio", 1.0),
            "no_speech_prob": s.get("no_speech_prob", 0.0),
            "audio_start": float(s.get("start", 0.0) or 0.0),
            "audio_end": float(s.get("end", 0.0) or 0.0),
        })

    text = data.get("text", "") or " ".join(s["text"].strip() for s in segments).strip()
    duration = data.get("duration")
    if duration is None:
        duration = segments[-1]["end"] if segments else 0.0

    return {
        "text": text.strip(),
        "language": _to_iso_language(data.get("language")) or fallback_language or "unknown",
        "language_probability": data.get("language_probability", 1.0),
        "duration": float(duration or 0.0),
        "segments": segments,
        "provider": "groq",
    }


def _normalize_deepgram(data: Dict[str, Any], fallback_language: Optional[str]) -> Dict[str, Any]:
    """Map Deepgram's /v1/listen response into the OpenAI-Whisper shape."""
    results = data.get("results", {}) or {}
    channels = results.get("channels", []) or []
    channel0 = channels[0] if channels else {}
    alternatives = channel0.get("alternatives", []) or []
    alt0 = alternatives[0] if alternatives else {}

    metadata = data.get("metadata", {}) or {}
    duration = float(metadata.get("duration", 0.0) or 0.0)

    language = _to_iso_language(channel0.get("detected_language")) or fallback_language or "unknown"
    lang_conf = channel0.get("language_confidence", 1.0)

    segments: List[Dict[str, Any]] = []
    utterances = results.get("utterances") or []
    if utterances:
        for idx, u in enumerate(utterances):
            segments.append(_dg_segment(
                idx,
                start=u.get("start", 0.0),
                end=u.get("end", 0.0),
                text=u.get("transcript", "") or "",
            ))
    elif alt0.get("transcript"):
        # No utterance breakdown: emit the whole alternative as one segment.
        segments.append(_dg_segment(
            0, start=0.0, end=duration, text=alt0.get("transcript", "") or "",
        ))

    text = alt0.get("transcript") or " ".join(s["text"].strip() for s in segments).strip()
    if not duration and segments:
        duration = segments[-1]["end"]

    return {
        "text": text.strip(),
        "language": language,
        "language_probability": float(lang_conf or 1.0),
        "duration": duration,
        "segments": segments,
        "provider": "deepgram",
    }


def _dg_segment(idx: int, start: float, end: float, text: str) -> Dict[str, Any]:
    start_f = float(start or 0.0)
    end_f = float(end or 0.0)
    # Deepgram gives no per-segment whisper confidences; use neutral values so
    # downstream quality filters never accidentally drop accepted speech.
    return {
        "id": idx,
        "seek": 0,
        "start": start_f,
        "end": end_f,
        "text": text,
        "tokens": [],
        "temperature": 0.0,
        "avg_logprob": 0.0,
        "compression_ratio": 1.0,
        "no_speech_prob": 0.0,
        "audio_start": start_f,
        "audio_end": end_f,
    }


def _utterance_language(u: Dict[str, Any]) -> Optional[str]:
    """Best-effort per-utterance language for a Deepgram 'multi' result.

    nova-3 multilingual tags each word with its language; pick the majority
    word language for the utterance so a Hindi sentence is stored as 'hi' and
    an English one as 'en' even within the same meeting. Returns None when no
    per-word language is present (caller falls back to the channel language).
    """
    counts: Dict[str, int] = {}
    for w in u.get("words") or []:
        lang = w.get("language")
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return None
    return _to_iso_language(max(counts, key=counts.get))


def _normalize_deepgram_diarized(
    data: Dict[str, Any], fallback_language: Optional[str]
) -> Dict[str, Any]:
    """Like _normalize_deepgram, but preserves the per-utterance speaker index
    from Deepgram diarization (utterances[].speaker -> int) and the per-utterance
    language from 'multi'. meeting-api maps speaker_index -> real name afterwards
    using the bot's active-speaker timeline."""
    results = data.get("results", {}) or {}
    channels = results.get("channels", []) or []
    channel0 = channels[0] if channels else {}
    alternatives = channel0.get("alternatives", []) or []
    alt0 = alternatives[0] if alternatives else {}

    metadata = data.get("metadata", {}) or {}
    duration = float(metadata.get("duration", 0.0) or 0.0)

    channel_language = _to_iso_language(channel0.get("detected_language")) or fallback_language or "multi"
    lang_conf = channel0.get("language_confidence", 1.0)

    segments: List[Dict[str, Any]] = []
    for idx, u in enumerate(results.get("utterances") or []):
        seg = _dg_segment(
            idx,
            start=u.get("start", 0.0),
            end=u.get("end", 0.0),
            text=u.get("transcript", "") or "",
        )
        spk = u.get("speaker")
        seg["speaker_index"] = int(spk) if spk is not None else None
        seg["language"] = _utterance_language(u) or channel_language
        segments.append(seg)

    text = alt0.get("transcript") or " ".join(s["text"].strip() for s in segments).strip()
    if not duration and segments:
        duration = segments[-1]["end"]

    return {
        "text": text.strip(),
        "language": channel_language,
        "language_probability": float(lang_conf or 1.0),
        "duration": duration,
        "segments": segments,
        "provider": "deepgram",
        "diarized": True,
    }


# --- Provider calls ----------------------------------------------------------
async def _transcribe_via_groq(
    client: httpx.AsyncClient,
    audio_bytes: bytes,
    filename: str,
    content_type: str,
    language: Optional[str],
    prompt: Optional[str],
    temperature: float,
) -> Dict[str, Any]:
    files = {"file": (filename, audio_bytes, content_type or "audio/wav")}
    form: Dict[str, str] = {
        "model": GROQ_MODEL,
        "response_format": "verbose_json",
        "temperature": str(temperature),
    }
    if language:
        form["language"] = language
    if prompt:
        # Groq caps the prompt at GROQ_MAX_PROMPT_CHARS; keep the tail so the
        # most recent context survives instead of 400-ing the whole request.
        if len(prompt) > GROQ_MAX_PROMPT_CHARS:
            prompt = prompt[-GROQ_MAX_PROMPT_CHARS:]
        form["prompt"] = prompt

    resp = await client.post(
        GROQ_API_URL,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        data=form,
        files=files,
    )
    resp.raise_for_status()
    return _normalize_groq(resp.json(), language)


async def _transcribe_via_deepgram(
    client: httpx.AsyncClient,
    audio_bytes: bytes,
    content_type: str,
    language: Optional[str],
) -> Dict[str, Any]:
    params: Dict[str, str] = {
        "model": DEEPGRAM_MODEL,
        "smart_format": "true",
        "punctuate": "true",
        "utterances": "true",  # gives us per-segment start/end without diarization
    }
    if language:
        params["language"] = language
    else:
        params["detect_language"] = "true"

    resp = await client.post(
        DEEPGRAM_API_URL,
        params=params,
        headers={
            "Authorization": f"Token {DEEPGRAM_API_KEY}",
            "Content-Type": content_type or "audio/wav",
        },
        content=audio_bytes,
    )
    resp.raise_for_status()
    return _normalize_deepgram(resp.json(), language)


async def _transcribe_via_deepgram_batch(
    client: httpx.AsyncClient,
    audio_bytes: bytes,
    content_type: str,
    language: str,
    diarize: bool,
) -> Dict[str, Any]:
    """Whole-file Deepgram call for the post-meeting batch path: one request,
    multilingual ('multi') by default, with speaker diarization."""
    params: Dict[str, str] = {
        "model": DEEPGRAM_MODEL,
        "smart_format": "true",
        "punctuate": "true",
        "utterances": "true",
        # Pin language (default 'multi') — never detect_language here; that is
        # exactly the per-chunk auto-detect that wrecked realtime quality.
        "language": language,
    }
    if diarize:
        params["diarize"] = "true"

    resp = await client.post(
        DEEPGRAM_API_URL,
        params=params,
        headers={
            "Authorization": f"Token {DEEPGRAM_API_KEY}",
            "Content-Type": content_type or "audio/webm",
        },
        content=audio_bytes,
    )
    resp.raise_for_status()
    return _normalize_deepgram_diarized(resp.json(), language)


async def transcribe_file_diarized(
    audio_bytes: bytes,
    content_type: str = "audio/webm",
    language: Optional[str] = None,
    diarize: bool = True,
) -> Dict[str, Any]:
    """Public entrypoint for the post-meeting batch transcript.

    Sends the WHOLE meeting audio to Deepgram once with 'multi' language +
    diarization. Deepgram-only by design (Groq Whisper has no diarization).
    Raises AllProvidersFailed if Deepgram is unconfigured or fails.
    """
    if not DEEPGRAM_API_KEY:
        raise AllProvidersFailed("deepgram: DEEPGRAM_API_KEY not set (required for batch diarized transcription)")

    lang = (language or DEEPGRAM_BATCH_LANGUAGE).strip() or DEEPGRAM_BATCH_LANGUAGE
    timeout = httpx.Timeout(DEEPGRAM_BATCH_TIMEOUT_SECONDS, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            return await _with_retries(
                "deepgram-batch",
                lambda: _transcribe_via_deepgram_batch(
                    client, audio_bytes, content_type, lang, diarize
                ),
            )
        except Exception as e:
            raise AllProvidersFailed(f"deepgram-batch: {_describe_http_error(e)}")


async def transcribe_via_providers(
    audio_bytes: bytes,
    filename: str = "audio.wav",
    content_type: str = "audio/wav",
    language: Optional[str] = None,
    prompt: Optional[str] = None,
    temperature: float = 0.0,
) -> Dict[str, Any]:
    """
    Try Groq first, fall back to Deepgram. Returns the Vexa-compatible response
    dict, or raises AllProvidersFailed if every configured provider failed.
    """
    errors: List[str] = []

    async with httpx.AsyncClient(timeout=PROVIDER_TIMEOUT_SECONDS) as client:
        # 1. Groq (primary) — skipped entirely when DEEPGRAM_ONLY is set.
        if not DEEPGRAM_ONLY:
            if GROQ_API_KEY:
                try:
                    return await _with_retries(
                        "groq",
                        lambda: _transcribe_via_groq(
                            client, audio_bytes, filename, content_type,
                            language, prompt, temperature,
                        ),
                    )
                except Exception as e:
                    detail = _describe_http_error(e)
                    if GROQ_ONLY:
                        logger.error(f"Groq transcription failed (GROQ_ONLY, no fallback): {detail}")
                        raise AllProvidersFailed(f"groq: {detail}")
                    logger.warning(f"Groq transcription failed, falling back to Deepgram: {detail}")
                    errors.append(f"groq: {detail}")
            else:
                errors.append("groq: GROQ_API_KEY not set")

        # 2. Deepgram (fallback, or sole provider when DEEPGRAM_ONLY) —
        #    skipped when GROQ_ONLY is set.
        if DEEPGRAM_API_KEY and not GROQ_ONLY:
            try:
                return await _with_retries(
                    "deepgram",
                    lambda: _transcribe_via_deepgram(
                        client, audio_bytes, content_type, language,
                    ),
                )
            except Exception as e:
                detail = _describe_http_error(e)
                if DEEPGRAM_ONLY:
                    logger.error(f"Deepgram transcription failed (DEEPGRAM_ONLY, no fallback): {detail}")
                    raise AllProvidersFailed(f"deepgram: {detail}")
                logger.error(f"Deepgram transcription failed: {detail}")
                errors.append(f"deepgram: {detail}")
        else:
            errors.append("deepgram: DEEPGRAM_API_KEY not set")

    raise AllProvidersFailed("; ".join(errors) or "no providers configured")


def _is_transient_error(e: Exception) -> bool:
    """True for failures worth retrying: network/timeout/protocol errors, plus
    HTTP 429 (rate limit) and 5xx. Deterministic 4xx (bad request, auth, payload
    too large) returns False — retrying would just reproduce the same failure."""
    if isinstance(e, httpx.HTTPStatusError):
        sc = e.response.status_code if e.response is not None else 0
        return sc == 429 or sc >= 500
    # httpx.TimeoutException is a subclass of TransportError, so this covers
    # timeouts, ConnectError, ReadError, RemoteProtocolError, etc.
    return isinstance(e, httpx.TransportError)


async def _with_retries(label: str, coro_factory):
    """Await coro_factory(), retrying transient failures with exponential
    backoff + jitter up to PROVIDER_MAX_RETRIES. Deterministic errors raise
    immediately. The final attempt's exception propagates to the caller."""
    attempt = 0
    while True:
        try:
            return await coro_factory()
        except Exception as e:
            attempt += 1
            if attempt > PROVIDER_MAX_RETRIES or not _is_transient_error(e):
                raise
            delay = PROVIDER_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            delay += random.uniform(0, PROVIDER_RETRY_BACKOFF_SECONDS)
            logger.warning(
                f"{label} transient failure (attempt {attempt}/{PROVIDER_MAX_RETRIES}), "
                f"retrying in {delay:.2f}s: {_describe_http_error(e)}"
            )
            await asyncio.sleep(delay)


def _describe_http_error(e: Exception) -> str:
    """Compact, log-safe description of a provider failure."""
    if isinstance(e, httpx.HTTPStatusError):
        body = e.response.text[:300] if e.response is not None else ""
        return f"HTTP {e.response.status_code}: {body}"
    if isinstance(e, httpx.TimeoutException):
        return "timeout"
    return f"{type(e).__name__}: {e}"
