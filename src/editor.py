"""Stage 6 — Video editing pipeline (FFmpeg only, no paid tools).

Applies in this order:
  1. Crop / scale to vertical 1080x1920
  2. Color grade: saturation boost, mild HDR look (curves), unsharp mask
  3. Mute original audio
  4. Mix in voiceover (full volume) + background music (ducked)
  5. Add a hook text overlay during the first 3 seconds
  6. Encode H.264 + AAC, fast preset, web-friendly

Subject-tracking arrows/circles are intentionally left as a future upgrade —
robust automated tracking needs heavy CV models that don't fit GitHub Actions
free minutes well. The hook overlay + grading already do most of the work.
"""
from __future__ import annotations

import logging
import random
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROCESSED_DIR = _PROJECT_ROOT / "data" / "processed"
MUSIC_DIR = _PROJECT_ROOT / "data" / "music"
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
MUSIC_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# Filter graph builder
# ============================================================

def _video_filter_chain(saturation: float, sharpen: bool, hdr_look: bool,
                        target_w: int, target_h: int) -> str:
    """Build the -vf filter string for grading + scale + crop to vertical."""
    parts: list[str] = []

    # Scale + crop to vertical: fit shortest side, then center-crop
    parts.append(
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
        f"crop={target_w}:{target_h}"
    )
    parts.append(f"eq=saturation={saturation:.2f}:contrast=1.05:brightness=0.02")
    if hdr_look:
        # Mild S-curve for HDR-ish punch
        parts.append("curves=preset=increase_contrast")
    if sharpen:
        parts.append("unsharp=5:5:0.8:3:3:0.4")
    parts.append("format=yuv420p")
    return ",".join(parts)


def _hook_drawtext(hook_text: str, duration: float = 3.0) -> str:
    """Drawtext filter for the first `duration` seconds."""
    safe = (hook_text or "").replace(":", "\\:").replace("'", "\\'")
    if not safe:
        return ""
    return (
        f"drawtext=text='{safe}'"
        f":fontcolor=white:fontsize=72:borderw=4:bordercolor=black"
        f":x=(w-text_w)/2:y=h*0.18"
        f":enable='between(t,0,{duration})'"
    )


def _pick_music(niche: str) -> Optional[Path]:
    """Pick a random track from data/music/<niche>/ or data/music/."""
    candidates: list[Path] = []
    sub = MUSIC_DIR / niche
    if sub.exists():
        candidates += list(sub.glob("*.mp3")) + list(sub.glob("*.wav"))
    if not candidates:
        candidates += list(MUSIC_DIR.glob("*.mp3")) + list(MUSIC_DIR.glob("*.wav"))
    if not candidates:
        return None
    return random.choice(candidates)


# ============================================================
# Public API
# ============================================================

def edit_video(
    *,
    source_video: Path,
    voiceover_audio: Path,
    video_id: int,
    niche: str,
    hook_text: str,
    target_resolution: tuple[int, int] = (1080, 1920),
    saturation: float = 1.25,
    sharpen: bool = True,
    hdr_look: bool = True,
    music_volume_db: float = -18.0,
) -> Optional[Path]:
    """Render the final Short. Returns output path or None on failure."""
    out = PROCESSED_DIR / f"{video_id}_final.mp4"
    if out.exists():
        out.unlink()

    target_w, target_h = target_resolution
    vf = _video_filter_chain(saturation, sharpen, hdr_look, target_w, target_h)
    hook = _hook_drawtext(hook_text)
    if hook:
        vf = f"{vf},{hook}"

    music = _pick_music(niche)
    cmd = ["ffmpeg", "-y", "-i", str(source_video), "-i", str(voiceover_audio)]
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]

    # Build audio filter graph
    if music:
        # vo = input 1, music = input 2 ducked under vo
        afilter = (
            f"[2:a]volume={music_volume_db}dB,aloop=loop=-1:size=2e+09[bg];"
            f"[1:a][bg]amix=inputs=2:duration=first:dropout_transition=2[a]"
        )
        cmd += [
            "-filter_complex", afilter,
            "-map", "0:v", "-map", "[a]",
        ]
    else:
        cmd += ["-map", "0:v", "-map", "1:a"]

    cmd += [
        "-vf", vf,
        "-r", "30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(out),
    ]

    try:
        subprocess.run(cmd, capture_output=True, check=True)
    except subprocess.CalledProcessError as e:
        log.error("FFmpeg failed:\n%s", e.stderr.decode("utf-8", errors="ignore")[:2000])
        return None
    return out if out.exists() else None
