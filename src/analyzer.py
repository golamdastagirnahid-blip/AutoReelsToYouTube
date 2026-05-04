"""Stage 3 — AI analysis of the video.

Approach (free-tier friendly):
  1. Probe duration with ffprobe.
  2. Extract N evenly-spaced keyframes with ffmpeg (JPEG, base64).
  3. Send frames + caption to NVIDIA NIM vision-capable LLM.
  4. Get back JSON: {topic, summary, scenes[], category, tone, target_audience, hook_idea}.

We use the OpenAI-compatible NVIDIA endpoint.
If the configured model isn't available, fall back to text-only with caption.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from openai import OpenAI

log = logging.getLogger(__name__)

_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
# Vision-capable models on NVIDIA NIM (tried in order until one works).
# Removed deprecated phi-3.5 and nvidia/vila (410 Gone).
_VISION_MODEL_CANDIDATES = [
    "meta/llama-3.2-11b-vision-instruct",
    "meta/llama-3.2-90b-vision-instruct",
    "google/gemma-3-27b-it",
    "qwen/qwen2.5-vl-32b-instruct",
]
_VISION_MODEL = _VISION_MODEL_CANDIDATES[0]
_TEXT_FALLBACK_MODEL = "meta/llama-3.1-70b-instruct"
# NVIDIA's vision endpoint allows max 1 image per request.
_MAX_FRAMES_PER_REQUEST = 1

_SYSTEM = (
    "You are a video analyst for a faceless YouTube Shorts channel. "
    "Given keyframes and the original caption, describe what is happening "
    "and produce strict JSON with these keys: "
    '{"topic": str, "summary": str, "scenes": [str], '
    '"category": str, "tone": str, "target_audience": str, '
    '"hook_idea": str, "safe_to_repurpose": bool, "reasons_unsafe": [str]} '
    "Set safe_to_repurpose=false if the video shows: identifiable celebrities, "
    "branded products, copyrighted music performance, sensitive content. "
    "Output ONLY JSON."
)


def probe_duration(video_path: Path) -> float:
    """Return duration in seconds using ffprobe."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True, text=True, check=True,
        )
        return float(r.stdout.strip())
    except Exception as e:
        log.warning("ffprobe failed: %s", e)
        return 0.0


def extract_keyframes(video_path: Path, n: int = 4) -> list[Path]:
    """Extract n evenly-spaced JPEG keyframes."""
    duration = probe_duration(video_path)
    if duration <= 0:
        return []
    out_dir = Path(tempfile.mkdtemp(prefix="frames_"))
    frames: list[Path] = []
    for i in range(n):
        t = duration * (i + 0.5) / n
        out = out_dir / f"frame_{i}.jpg"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(video_path),
                    "-frames:v", "1", "-q:v", "3", "-vf", "scale=512:-2",
                    str(out),
                ],
                capture_output=True, check=True,
            )
            if out.exists():
                frames.append(out)
        except Exception as e:
            log.debug("Frame extract failed at t=%.2f: %s", t, e)
    return frames


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _parse_json(text: str) -> dict:
    """Extract the first JSON object from a model response."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError(f"No JSON found in response: {text[:200]}")
    return json.loads(match.group(0))


def analyze_video(
    video_path: Path,
    caption: str,
    nvidia_api_key: str,
    *,
    model: str = _VISION_MODEL,
    fallback_model: str = _TEXT_FALLBACK_MODEL,
) -> dict:
    """Returns the analysis dict (and adds duration_sec)."""
    duration = probe_duration(video_path)
    # Sample more frames for selection but only send 1 to the API (vision limit)
    all_frames = extract_keyframes(video_path, n=4)
    # Pick the middle frame as the most representative
    frames = [all_frames[len(all_frames) // 2]] if all_frames else []
    frames = frames[:_MAX_FRAMES_PER_REQUEST]

    client = OpenAI(api_key=nvidia_api_key, base_url=_NVIDIA_BASE_URL)

    # Try vision model first if we have frames
    if frames:
        content = [
            {"type": "text",
             "text": f"Caption: {caption or '(none)'}\nDuration: {duration:.1f}s\n"
                     f"Analyze this representative frame and respond with strict JSON."},
        ]
        for fp in frames:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{_b64(fp)}"},
            })

        # Try the configured model, then each candidate vision model
        tried = []
        for candidate in [model] + [m for m in _VISION_MODEL_CANDIDATES if m != model]:
            if candidate in tried:
                continue
            tried.append(candidate)
            try:
                resp = client.chat.completions.create(
                    model=candidate,
                    messages=[
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": content},
                    ],
                    temperature=0.4,
                    max_tokens=800,
                )
                data = _parse_json(resp.choices[0].message.content)
                data["duration_sec"] = duration
                log.info("Vision analysis OK with model=%s", candidate)
                return data
            except Exception as e:
                msg = str(e)[:120]
                if "404" in msg or "not found" in msg.lower() or "unknown model" in msg.lower():
                    log.debug("Vision model %s unavailable, trying next", candidate)
                    continue
                log.warning("Vision analysis failed on %s (%s); trying next", candidate, msg)
        log.warning("All vision models failed; falling back to text-only")

    # Text-only fallback
    resp = client.chat.completions.create(
        model=fallback_model,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user",
             "content": f"Caption: {caption or '(none)'}\nDuration: {duration:.1f}s\n"
                        f"No frames available. Infer from caption only and respond with strict JSON."},
        ],
        temperature=0.5,
        max_tokens=600,
    )
    data = _parse_json(resp.choices[0].message.content)
    data["duration_sec"] = duration
    return data
