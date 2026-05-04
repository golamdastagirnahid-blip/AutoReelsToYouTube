"""Stage 5 — Text-to-speech with multi-backend fallback.

Uses the ai33.pro proxy which exposes three TTS engines under one API key:
  1. ElevenLabs-compatible  → /v1/text-to-speech/<voice_id>   (premium quality)
  2. MiniMax                → /v1m/task/text-to-speech        (good quality)
  3. Microsoft Edge Neural  → /v1e/task/text-to-speech        (free, unlimited)

On each call we try them in order until one succeeds.
Automatically adjusts audio speed to match target video duration.
"""
from __future__ import annotations

import base64
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
VO_DIR = _PROJECT_ROOT / "data" / "processed"
VO_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL = "https://api.ai33.pro"

# Per-backend deadlines. Tightened from earlier values — the slow proxy
# backends rarely succeed, so don't burn 5 min waiting. If they're going
# to work, they work fast.
PER_BACKEND_TIMEOUT_SEC = {
    "edge":       120,   # almost always 5-15 s, very reliable
    "minimax":    90,    # was 240, cut to fail fast on bad days
    "elevenlabs": 90,    # was 300, ai33.pro proxy is unreliable
}
HTTP_REQUEST_TIMEOUT_SEC = 45
POLL_INTERVAL_SEC = 2

# Edge first — it's free, unlimited, neural-quality, and works 100% of the
# time. The proxy backends (minimax, elevenlabs) are kept as fallback for
# variety / quality preference, but smart-backoff means they're skipped
# automatically after consecutive failures (see _backend_skipped).
DEFAULT_BACKEND_ORDER = ["edge", "minimax", "elevenlabs"]

# Smart-backoff config
FAIL_THRESHOLD = 3              # consecutive failures before skipping
FAIL_BACKOFF_HOURS = 24         # how long to skip after threshold reached

# Default voices per backend (override via .env or args)
# Brian = Microsoft's most realistic, popular male English voice — widely used
# in faceless YouTube channels. Natural prosody, warm conversational tone.
DEFAULT_EDGE_VOICE = "en-US-BrianMultilingualNeural"
DEFAULT_MINIMAX_VOICE = "209533299589184"   # MiniMax default English male
DEFAULT_MINIMAX_MODEL = "speech-2.6-hd"


# ------------------------------------------------------------
# Response parsing — the proxy may return bytes OR JSON
# ------------------------------------------------------------

def _is_audio_bytes(content: bytes) -> bool:
    """Heuristic: MP3 starts with ID3 or 0xFFFB/0xFFF3, WAV with RIFF."""
    if not content or len(content) < 100:
        return False
    head = content[:4]
    if head.startswith(b"ID3") or head.startswith(b"RIFF"):
        return True
    if len(content) > 2 and content[0] == 0xFF and content[1] in (0xFB, 0xF3, 0xF2):
        return True
    return False


def _extract_audio_from_json(data: dict, headers: dict, session: requests.Session,
                             out_path: Path, deadline: float) -> Optional[Path]:
    """Handle common async/sync JSON shapes and write audio to out_path.

    `deadline` is a time.monotonic() value; if reached, abort.

    ai33.pro shape:
      { "status": "done", "progress": 100,
        "metadata": { "audio_url": "https://cdn.ai33.pro/...mp3" } }
    """
    # Unwrap common envelope keys (ai33.pro nests audio_url under "metadata")
    for k in ("data", "result", "response", "metadata"):
        if isinstance(data.get(k), dict):
            data = {**data, **data[k]}

    # 1. Direct URL to audio
    for url_key in ("audio_url", "url", "file_url", "audio", "output_url"):
        url = data.get(url_key)
        if isinstance(url, str) and url.startswith("http"):
            if time.monotonic() > deadline:
                return None
            try:
                r = session.get(url, timeout=HTTP_REQUEST_TIMEOUT_SEC)
                r.raise_for_status()
                out_path.write_bytes(r.content)
                return out_path
            except Exception as e:
                log.warning("Failed to download audio from %s: %s", url, e)

    # 2. Base64 payload
    for b64_key in ("audio_base64", "audio_b64", "base64"):
        b64 = data.get(b64_key)
        if isinstance(b64, str) and len(b64) > 200:
            try:
                out_path.write_bytes(base64.b64decode(b64))
                return out_path
            except Exception as e:
                log.warning("Failed to decode base64: %s", e)

    # 3. Task-based async — poll task_id until deadline
    task_id = data.get("task_id") or data.get("id")
    if task_id:
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_SEC)
            try:
                q = session.get(f"{BASE_URL}/v1/task/{task_id}",
                                headers=headers, timeout=HTTP_REQUEST_TIMEOUT_SEC)
                if q.status_code != 200:
                    continue
                qd = q.json()
                if qd.get("status") in ("done", "finished", "success", "completed"):
                    return _extract_audio_from_json(qd, headers, session, out_path, deadline)
                if qd.get("status") in ("failed", "error"):
                    log.warning("Task %s reported failed: %s", task_id, str(qd)[:200])
                    return None
            except Exception:
                continue
        log.warning("Task %s polling timed out (deadline reached)", task_id)
    return None


def _post_tts(path: str, payload: dict, api_key: str, out: Path,
              deadline: float) -> Optional[Path]:
    """Generic TTS POST. Handles both binary and JSON responses, respects deadline."""
    if time.monotonic() > deadline:
        return None
    url = f"{BASE_URL}{path}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg, application/json",
    }
    sess = requests.Session()
    remaining = max(5.0, deadline - time.monotonic())
    try:
        r = sess.post(url, headers=headers, json=payload,
                      timeout=min(HTTP_REQUEST_TIMEOUT_SEC, remaining))
    except Exception as e:
        log.warning("TTS request to %s failed: %s", url, e)
        return None
    if r.status_code >= 400:
        log.warning("TTS %s returned %s: %s", url, r.status_code, r.text[:200])
        return None
    if _is_audio_bytes(r.content):
        out.write_bytes(r.content)
        return out
    try:
        data = r.json()
    except Exception:
        log.warning("TTS %s returned unknown format (not audio, not JSON)", url)
        return None
    return _extract_audio_from_json(data, headers, sess, out, deadline)


# ------------------------------------------------------------
# Backend: ElevenLabs (via proxy)
# ------------------------------------------------------------

def _tts_elevenlabs(text: str, api_key: str, voice_id: str, out: Path,
                   model: str, deadline: float) -> Optional[Path]:
    path = f"/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128"
    payload = {"text": text, "model_id": model}
    return _post_tts(path, payload, api_key, out, deadline)


# ------------------------------------------------------------
# Backend: MiniMax (via proxy)
# ------------------------------------------------------------

def _tts_minimax(text: str, api_key: str, out: Path, deadline: float,
                voice_id: str = DEFAULT_MINIMAX_VOICE,
                model: str = DEFAULT_MINIMAX_MODEL,
                speed: float = 1.0) -> Optional[Path]:
    payload = {
        "text": text,
        "model": model,
        "voice_setting": {
            "voice_id": voice_id, "vol": 1, "pitch": 0, "speed": speed,
        },
        "language_boost": "Auto",
    }
    return _post_tts("/v1m/task/text-to-speech", payload, api_key, out, deadline)


# ------------------------------------------------------------
# Backend: Edge Neural (via proxy)
# ------------------------------------------------------------

def _tts_edge(text: str, api_key: str, out: Path, deadline: float,
              voice: str = DEFAULT_EDGE_VOICE,
              speed: float = 1.0) -> Optional[Path]:
    payload = {
        "text": text,
        "voice": voice,
        "speed": speed,
        "with_transcript": False,
        "with_loudnorm": True,
    }
    return _post_tts("/v1e/task/text-to-speech", payload, api_key, out, deadline)


# ------------------------------------------------------------
# Public entry — tries ElevenLabs → MiniMax → Edge
# ------------------------------------------------------------

# ------------------------------------------------------------
# Smart 24h backoff for unreliable backends
# Persists to Tracker.kv_state so it survives across runs.
# ------------------------------------------------------------

def _backend_skipped(tracker, name: str) -> bool:
    """True if this backend has failed >=FAIL_THRESHOLD times in a row
    AND the last failure was within FAIL_BACKOFF_HOURS.
    """
    if tracker is None:
        return False
    try:
        cnt = int(tracker.kv_get(f"tts_fail_count:{name}") or 0)
        last = tracker.kv_get(f"tts_failed_at:{name}")
        if cnt < FAIL_THRESHOLD or not last:
            return False
        from datetime import datetime, timedelta
        last_dt = datetime.fromisoformat(last)
        hours_since = (datetime.utcnow() - last_dt).total_seconds() / 3600
        return hours_since < FAIL_BACKOFF_HOURS
    except Exception:
        return False


def _record_failure(tracker, name: str) -> None:
    if tracker is None:
        return
    try:
        from datetime import datetime
        cnt = int(tracker.kv_get(f"tts_fail_count:{name}") or 0) + 1
        tracker.kv_set(f"tts_fail_count:{name}", str(cnt))
        tracker.kv_set(f"tts_failed_at:{name}", datetime.utcnow().isoformat())
    except Exception:
        pass


def _record_success(tracker, name: str) -> None:
    if tracker is None:
        return
    try:
        tracker.kv_set(f"tts_fail_count:{name}", "0")
    except Exception:
        pass


def synthesize(
    text: str,
    video_id: int,
    api_key: str,
    voice_id: str,
    *,
    model: str = "eleven_multilingual_v2",
    stability: float = 0.5,
    similarity_boost: float = 0.75,
    style: float = 0.4,
    speaker_boost: bool = True,
    backend_order: Optional[list] = None,
    tracker=None,
) -> Optional[Path]:
    """Generate voiceover with automatic backend fallback. Returns MP3 path.

    Args:
        backend_order: override default order (e.g. from config.yaml).
        tracker: optional Tracker for smart 24h backoff on failing backends.
    """
    out = VO_DIR / f"{video_id}_vo.mp3"
    if out.exists():
        out.unlink()

    fns = {
        "elevenlabs": lambda dl: _tts_elevenlabs(text, api_key, voice_id, out, model, dl),
        "minimax":    lambda dl: _tts_minimax(text, api_key, out, dl),
        "edge":       lambda dl: _tts_edge(text, api_key, out, dl),
    }
    order = backend_order or DEFAULT_BACKEND_ORDER
    for name in order:
        fn = fns.get(name)
        if fn is None:
            continue
        if _backend_skipped(tracker, name):
            log.info("TTS backend %s skipped (recent consecutive failures, "
                     "backing off %dh)", name, FAIL_BACKOFF_HOURS)
            continue
        cap = PER_BACKEND_TIMEOUT_SEC.get(name, 120)
        deadline = time.monotonic() + cap
        log.info("TTS attempt: %s (cap %ds)", name, cap)
        started = time.monotonic()
        try:
            result = fn(deadline)
        except Exception as e:
            log.warning("TTS backend %s raised: %s", name, e)
            result = None
        elapsed = time.monotonic() - started
        if result and result.exists() and result.stat().st_size > 1000:
            log.info("TTS succeeded with backend=%s in %.1fs (%.1f KB)",
                     name, elapsed, result.stat().st_size / 1024)
            _record_success(tracker, name)
            return result
        log.info("TTS backend %s gave up after %.1fs", name, elapsed)
        _record_failure(tracker, name)
        if out.exists():
            out.unlink()

    log.error("All TTS backends failed")
    return None


def audio_duration(path: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def fit_audio_to_duration(audio_path: Path, target_sec: float) -> Path:
    """Adjust audio playback speed (atempo) so that its duration matches target.

    Uses ffmpeg atempo filter, valid range 0.5–2.0.
    """
    cur = audio_duration(audio_path)
    if cur <= 0 or target_sec <= 0:
        return audio_path
    ratio = cur / target_sec  # >1 means audio is too long → speed up
    ratio = max(0.85, min(1.20, ratio))  # keep natural range
    if abs(ratio - 1.0) < 0.03:
        return audio_path

    out = audio_path.with_name(audio_path.stem + "_fit.mp3")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(audio_path),
             "-filter:a", f"atempo={ratio:.3f}",
             "-vn", str(out)],
            capture_output=True, check=True,
        )
        return out if out.exists() else audio_path
    except Exception as e:
        log.warning("atempo fit failed: %s", e)
        return audio_path
