"""Stage 1 — Reel/video discovery.

Two sources:
  1. Instagram trending Reels (hashtag-based, individual creators only)
  2. Copyright-free stock video providers (Pexels, Pixabay)

Returns a unified list of `Candidate` dicts to be downloaded next.
"""
from __future__ import annotations

import logging
import random
from dataclasses import asdict, dataclass
from typing import Optional

import requests

from src.utils.safety import is_corporate_handle, load_blocklist

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    source_type: str           # 'instagram' | 'pexels' | 'pixabay'
    source_url: str
    creator_handle: Optional[str]
    creator_url: Optional[str]
    caption: Optional[str]
    hashtags: Optional[str]
    duration_sec: Optional[float]
    niche: str

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
# Instagram (via instaloader hashtag scraping)
# ============================================================

def discover_instagram(
    keywords: list[str],
    niche: str,
    *,
    min_views: int = 100_000,
    min_like_ratio: float = 0.03,
    max_duration: int = 60,
    max_results: int = 10,
    exclude_verified: bool = True,
    instagram_username: Optional[str] = None,
    instagram_password: Optional[str] = None,
) -> list[Candidate]:
    """Find trending Reels for a niche via instaloader.

    Note: Instagram heavily rate-limits scrapers. We:
      - use small per-hashtag limits
      - rotate hashtags
      - skip corporate / verified accounts
      - skip blocklisted creators
    """
    try:
        import instaloader
    except ImportError:
        log.error("instaloader not installed; skipping Instagram discovery")
        return []

    L = instaloader.Instaloader(
        download_videos=False,
        download_video_thumbnails=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )

    if instagram_username and instagram_password:
        try:
            L.login(instagram_username, instagram_password)
        except Exception as e:
            log.warning("Instagram login failed: %s — continuing anonymously", e)

    blocked = load_blocklist()
    out: list[Candidate] = []
    random.shuffle(keywords)

    for kw in keywords:
        if len(out) >= max_results:
            break
        tag = kw.replace(" ", "").lower()
        log.info("Scanning Instagram hashtag: #%s", tag)
        try:
            hashtag = instaloader.Hashtag.from_name(L.context, tag)
            checked = 0
            for post in hashtag.get_top_posts():
                if checked >= 30 or len(out) >= max_results:
                    break
                checked += 1
                try:
                    if not post.is_video:
                        continue
                    duration = getattr(post, "video_duration", None) or 0
                    if duration and duration > max_duration:
                        continue
                    views = post.video_view_count or 0
                    likes = post.likes or 0
                    if views < min_views:
                        continue
                    if views and likes / views < min_like_ratio:
                        continue

                    owner = post.owner_username or ""
                    is_verified = bool(getattr(post.owner_profile, "is_verified", False))
                    if exclude_verified and is_corporate_handle(owner, is_verified):
                        continue
                    if owner.lower().lstrip("@") in blocked:
                        continue

                    cap = post.caption or ""
                    tags = " ".join(f"#{t}" for t in (post.caption_hashtags or [])[:15])

                    out.append(Candidate(
                        source_type="instagram",
                        source_url=f"https://www.instagram.com/reel/{post.shortcode}/",
                        creator_handle=owner,
                        creator_url=f"https://www.instagram.com/{owner}/",
                        caption=cap[:500],
                        hashtags=tags,
                        duration_sec=float(duration) if duration else None,
                        niche=niche,
                    ))
                except Exception as e:
                    log.debug("Skipping post: %s", e)
                    continue
        except Exception as e:
            log.warning("Hashtag #%s failed: %s", tag, e)
            continue

    log.info("Instagram discovery: %d candidates for niche=%s", len(out), niche)
    return out


# ============================================================
# Pexels (free, copyright-free)
# ============================================================

def discover_pexels(
    keywords: list[str],
    niche: str,
    api_key: str,
    *,
    per_page: int = 5,
    max_results: int = 5,
) -> list[Candidate]:
    if not api_key:
        return []
    out: list[Candidate] = []
    headers = {"Authorization": api_key}
    for kw in keywords:
        if len(out) >= max_results:
            break
        try:
            r = requests.get(
                "https://api.pexels.com/videos/search",
                headers=headers,
                params={"query": kw, "per_page": per_page, "orientation": "portrait"},
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("Pexels fetch failed for %s: %s", kw, e)
            continue
        for v in data.get("videos", []):
            if len(out) >= max_results:
                break
            files = v.get("video_files", [])
            best = None
            for f in files:
                if (f.get("height") or 0) >= 1080:
                    best = f
                    break
            if not best and files:
                best = files[0]
            if not best:
                continue
            out.append(Candidate(
                source_type="pexels",
                source_url=best["link"],
                creator_handle=v.get("user", {}).get("name"),
                creator_url=v.get("user", {}).get("url"),
                caption=f"Pexels video by {v.get('user', {}).get('name', 'unknown')}",
                hashtags=f"#{kw.replace(' ', '')}",
                duration_sec=float(v.get("duration") or 0),
                niche=niche,
            ))
    log.info("Pexels discovery: %d candidates for niche=%s", len(out), niche)
    return out


# ============================================================
# Pixabay (free, copyright-free)
# ============================================================

def discover_pixabay(
    keywords: list[str],
    niche: str,
    api_key: str,
    *,
    max_results: int = 5,
) -> list[Candidate]:
    if not api_key:
        return []
    out: list[Candidate] = []
    for kw in keywords:
        if len(out) >= max_results:
            break
        try:
            r = requests.get(
                "https://pixabay.com/api/videos/",
                params={"key": api_key, "q": kw, "per_page": 5, "safesearch": "true"},
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("Pixabay fetch failed for %s: %s", kw, e)
            continue
        for v in data.get("hits", []):
            if len(out) >= max_results:
                break
            videos = v.get("videos", {})
            best = videos.get("large") or videos.get("medium") or videos.get("small")
            if not best or not best.get("url"):
                continue
            out.append(Candidate(
                source_type="pixabay",
                source_url=best["url"],
                creator_handle=v.get("user"),
                creator_url=f"https://pixabay.com/users/{v.get('user', '')}-{v.get('user_id', '')}/",
                caption=f"Pixabay video by {v.get('user', 'unknown')}",
                hashtags=" ".join(f"#{t.strip()}" for t in (v.get("tags") or "").split(",")[:8]),
                duration_sec=float(v.get("duration") or 0),
                niche=niche,
            ))
    log.info("Pixabay discovery: %d candidates for niche=%s", len(out), niche)
    return out
