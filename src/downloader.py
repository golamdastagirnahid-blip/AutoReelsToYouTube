"""Stage 2 — Download the candidate video and store metadata.

Routing:
  - Instagram                → yt-dlp (auth + formats)
  - TikTok via TikWM         → plain HTTP (download_url is direct mp4)
  - TikTok webpage URL only  → yt-dlp fallback
  - Pexels / Pixabay video   → plain HTTP
  - Image (Pexels/Pixabay)   → HTTP, then convert to Ken-Burns video
"""
from __future__ import annotations

import logging
import shutil
import subprocess
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
    return _download_candidate(candidate, target)


def download_to(url: str, target: Path,
                source_type: str = "http",
                media_kind: str = "video") -> Optional[Path]:
    """Lower-level: download a known URL to an explicit target path."""
    if target.exists():
        target.unlink()
    if media_kind == "image":
        img = _download_http(url, target.with_suffix(".jpg"))
        if not img:
            return None
        return _image_to_kenburns_clip(img, target)
    if source_type == "instagram":
        return _download_with_ytdlp(url, target)
    if source_type == "tiktok":
        # If we got a tikwm direct mp4 URL, plain HTTP works.
        if "tiktokcdn" in url or "tikwm" in url or url.endswith(".mp4"):
            return _download_http(url, target)
        return _download_with_ytdlp(url, target)
    return _download_http(url, target)


def _download_candidate(c: Candidate, target: Path) -> Optional[Path]:
    """Smart-route a Candidate based on its media_kind and source."""
    fetch_url = c.fetch_url
    if c.media_kind == "image":
        img = _download_http(fetch_url, target.with_suffix(".jpg"))
        if not img:
            return None
        return _image_to_kenburns_clip(img, target)
    if c.source_type == "instagram":
        return _download_with_ytdlp(fetch_url, target)
    if c.source_type == "tiktok":
        # Prefer direct mp4 (tikwm) → HTTP. Fall back to yt-dlp.
        if c.download_url:
            return _download_http(c.download_url, target)
        return _download_with_ytdlp(fetch_url, target)
    return _download_http(fetch_url, target)


# ============================================================
# Image → Ken Burns video clip
# ============================================================

def _image_to_kenburns_clip(image_path: Path, target_mp4: Path,
                             *, duration: float = 4.5,
                             target_w: int = 1080,
                             target_h: int = 1920) -> Optional[Path]:
    """Convert a still image into a 1080x1920 vertical clip with Ken Burns zoom.

    Uses zoompan filter for slow continuous zoom-in (1.00 → 1.10 over `duration`).
    Output is a normalized MP4 ready for the multiclip editor.
    """
    if target_mp4.exists():
        target_mp4.unlink()
    fps = 30
    frames = int(duration * fps)
    # Direction picked deterministically from filename for variety across clips
    seed = sum(ord(c) for c in image_path.name)
    pan_pattern = seed % 4
    if pan_pattern == 0:
        zoom_expr = "z='zoom+0.0015'"
        x_expr = "x='iw/2-(iw/zoom/2)'"
        y_expr = "y='ih/2-(ih/zoom/2)'"
    elif pan_pattern == 1:
        zoom_expr = "z='zoom+0.0015'"
        x_expr = "x='(iw-iw/zoom)*on/(d-1)'"     # pan left → right
        y_expr = "y='ih/2-(ih/zoom/2)'"
    elif pan_pattern == 2:
        zoom_expr = "z='zoom+0.0015'"
        x_expr = "x='iw/2-(iw/zoom/2)'"
        y_expr = "y='(ih-ih/zoom)*on/(d-1)'"     # pan top → bottom
    else:
        zoom_expr = "z='if(eq(on,1),1.10,zoom-0.0015)'"  # zoom out
        x_expr = "x='iw/2-(iw/zoom/2)'"
        y_expr = "y='ih/2-(ih/zoom/2)'"

    vf = (
        f"scale={target_w*2}:{target_h*2}:flags=lanczos:"
        f"force_original_aspect_ratio=increase,"
        f"crop={target_w*2}:{target_h*2},"
        f"zoompan={zoom_expr}:{x_expr}:{y_expr}:"
        f"d={frames}:s={target_w}x{target_h}:fps={fps}"
    )
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-t", f"{duration:.3f}",
        "-i", str(image_path),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-movflags", "+faststart",
        str(target_mp4),
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=60)
    except subprocess.CalledProcessError as e:
        log.warning("Ken-Burns conversion failed: %s",
                    e.stderr.decode("utf-8", errors="ignore")[:400])
        return None
    except subprocess.TimeoutExpired:
        log.warning("Ken-Burns conversion timed out")
        return None
    return target_mp4 if target_mp4.exists() else None


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


_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": ("image/avif,image/webp,image/apng,image/*,video/*,"
               "*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
}


def _download_http(url: str, target: Path) -> Optional[Path]:
    try:
        with requests.get(url, stream=True, timeout=60,
                          headers=_BROWSER_HEADERS,
                          allow_redirects=True) as r:
            r.raise_for_status()
            with open(target, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    if chunk:
                        f.write(chunk)
        # Quick sanity: hotlinked Google images sometimes return tiny
        # placeholder bytes (1x1 GIF) for blocked referrers.
        if target.exists() and target.stat().st_size < 5_000:
            log.warning("HTTP download too small (%d bytes) — likely blocked: %s",
                        target.stat().st_size, url)
            target.unlink()
            return None
        return target
    except Exception as e:
        log.error("HTTP download failed for %s: %s", url, e)
        if target.exists():
            target.unlink()
        return None


def hash_file(path: Path) -> str:
    return sha256_file(path)
