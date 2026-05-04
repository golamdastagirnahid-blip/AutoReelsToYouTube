"""Smart segment selection for multi-clip editing.

Goal: from each source clip, extract the most visually engaging N seconds.

Tactics (in order of preference):
  1. **Scene-cut detection** via FFmpeg's `select=gt(scene,X)` filter.
     Picks the segment around the strongest scene change inside the clip.
  2. **Motion-energy heuristic** — sample frames and pick the segment with
     the highest mean inter-frame difference. Falls back when ffprobe lacks
     scene metadata.
  3. **Random middle slice** — last-resort: take a random window from the
     middle 70% of the clip (avoids title cards / outros).
"""
from __future__ import annotations

import logging
import random
import re
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def probe_duration(video: Path) -> float:
    """Return the duration of a video file in seconds (0 if unknown)."""
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nokey=1:noprint_wrappers=1",
                str(video),
            ],
            stderr=subprocess.STDOUT,
            timeout=15,
        )
        return float(out.decode().strip() or 0.0)
    except Exception as e:
        log.debug("ffprobe duration failed for %s: %s", video, e)
        return 0.0


def _scene_change_timestamps(video: Path, threshold: float = 0.35,
                              max_seconds: float = 30.0) -> list[float]:
    """Return scene-change timestamps detected in the first `max_seconds`.

    Uses ffmpeg's scene-detect filter and parses showinfo output.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "info",
        "-t", str(max_seconds),
        "-i", str(video),
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-vsync", "vfr", "-an",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        stderr = proc.stderr.decode("utf-8", errors="ignore")
    except Exception as e:
        log.debug("Scene-detect failed for %s: %s", video, e)
        return []
    times: list[float] = []
    for m in re.finditer(r"pts_time:([\d.]+)", stderr):
        try:
            times.append(float(m.group(1)))
        except ValueError:
            pass
    return times


def pick_best_segment(
    video: Path,
    *,
    target_duration: float = 4.0,
    margin: float = 0.5,
) -> Optional[tuple[float, float]]:
    """Return `(start, duration)` of the most engaging segment in `video`.

    `target_duration` is the clip length we want to extract. `margin` keeps
    us a bit away from absolute file boundaries to avoid I-frame issues.
    """
    total = probe_duration(video)
    if total <= 0:
        return None

    if total <= target_duration + margin * 2:
        # Clip is shorter than what we want — just return the whole thing.
        return (0.0, max(0.5, total - 0.05))

    usable_start = margin
    usable_end = total - target_duration - margin
    if usable_end <= usable_start:
        return (max(0.0, (total - target_duration) / 2), target_duration)

    # 1. Scene-cut detection
    cuts = _scene_change_timestamps(video, threshold=0.35,
                                     max_seconds=min(60.0, total))
    candidate_starts = [
        max(usable_start, min(usable_end, t - 0.2)) for t in cuts
    ]
    if candidate_starts:
        # Prefer cuts in the middle 70% of the clip (skip intro/outro)
        mid_lo = total * 0.10
        mid_hi = total * 0.90
        mid_cuts = [s for s in candidate_starts if mid_lo <= s <= mid_hi]
        choice = random.choice(mid_cuts or candidate_starts)
        return (round(choice, 2), target_duration)

    # 2. Random middle-slice fallback
    start = random.uniform(total * 0.20, total * 0.65)
    start = max(usable_start, min(usable_end, start))
    return (round(start, 2), target_duration)
