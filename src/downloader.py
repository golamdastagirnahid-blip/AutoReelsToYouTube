"""Stage 2 — Download the candidate video and store metadata.

Uses yt-dlp for Instagram (handles auth, formats, etc.).
For Pexels/Pixabay direct URLs, uses plain HTTP download.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

import requests

from src.discovery import Candidate
from src.utils.safety import sha256_file

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOWNLOAD_DIR = _PROJECT_ROOT / "data" / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def download(candidate: Candidate, video_id: int) -> Optional[Path]:
    """Download to data/downloads/<video_id>.mp4. Returns path or None."""
    target = DOWNLOAD_DIR / f"{video_id}.mp4"
    if target.exists():
        target.unlink()

    if candidate.source_type == "instagram":
        return _download_with_ytdlp(candidate.source_url, target)
    return _download_http(candidate.source_url, target)


def _download_with_ytdlp(url: str, target: Path) -> Optional[Path]:
    try:
        import yt_dlp
    except ImportError:
        log.error("yt-dlp not installed")
        return None

    opts = {
        "outtmpl": str(target.with_suffix(".%(ext)s")),
        "format": "mp4/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        log.error("yt-dlp failed for %s: %s", url, e)
        return None

    # yt-dlp may use a different extension; find it
    candidates = list(target.parent.glob(f"{target.stem}.*"))
    candidates = [p for p in candidates if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}]
    if not candidates:
        return None
    src = candidates[0]
    if src != target:
        if target.exists():
            target.unlink()
        shutil.move(str(src), str(target))
    return target if target.exists() else None


def _download_http(url: str, target: Path) -> Optional[Path]:
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(target, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    if chunk:
                        f.write(chunk)
        return target
    except Exception as e:
        log.error("HTTP download failed for %s: %s", url, e)
        if target.exists():
            target.unlink()
        return None


def hash_file(path: Path) -> str:
    return sha256_file(path)
