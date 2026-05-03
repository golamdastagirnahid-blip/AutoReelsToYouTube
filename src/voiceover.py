"""Stage 5 — Text-to-speech via ElevenLabs.

Generates an MP3 voiceover from the script text and tries to match
the target video duration by adjusting speech speed slightly.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
VO_DIR = _PROJECT_ROOT / "data" / "processed"
VO_DIR.mkdir(parents=True, exist_ok=True)

_ELEVEN_BASE = "https://api.elevenlabs.io/v1"


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
) -> Optional[Path]:
    """Generate VO and return the MP3 path."""
    out = VO_DIR / f"{video_id}_vo.mp3"
    payload = {
        "text": text,
        "model_id": model,
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity_boost,
            "style": style,
            "use_speaker_boost": speaker_boost,
        },
    }
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    try:
        r = requests.post(
            f"{_ELEVEN_BASE}/text-to-speech/{voice_id}",
            headers=headers,
            json=payload,
            timeout=120,
        )
        r.raise_for_status()
        out.write_bytes(r.content)
        return out
    except Exception as e:
        log.error("ElevenLabs TTS failed: %s", e)
        if out.exists():
            out.unlink()
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
