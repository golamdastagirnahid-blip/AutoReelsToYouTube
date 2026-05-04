"""Stage 1 — Reel/video discovery.

Sources:
  1. Instagram Reels (curated creator profiles, requires aged login)
  2. TikTok via TikWM public API (watermark-free, no auth)
  3. Pexels / Pixabay videos and photos (free APIs)

Returns a unified list of `Candidate` dicts to be downloaded next.
"""
from __future__ import annotations

import logging
import random
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

import requests

from src.utils.safety import is_corporate_handle, load_blocklist

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    source_type: str           # 'instagram' | 'tiktok' | 'pexels' | 'pixabay' | 'image'
    source_url: str            # Canonical webpage URL (for dedup + attribution)
    creator_handle: Optional[str]
    creator_url: Optional[str]
    caption: Optional[str]
    hashtags: Optional[str]
    duration_sec: Optional[float]
    niche: str
    # Optional direct media URL when different from source_url.
    # Used for TikWM (watermark-free MP4) and Pexels/Pixabay direct CDN links.
    download_url: Optional[str] = None
    # 'video' (default) | 'image' — drives downloader / segment behaviour.
    media_kind: str = "video"
    # Free-text label of the search concept that led us to this clip
    # (helps multi-angle ranking later).
    concept: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def fetch_url(self) -> str:
        """The URL the downloader should actually GET."""
        return self.download_url or self.source_url


# ============================================================
# Instagram (via instaloader hashtag scraping)
# ============================================================

_PROJECT_ROOT_FOR_SESSION = None  # set lazily

def _session_path():
    global _PROJECT_ROOT_FOR_SESSION
    if _PROJECT_ROOT_FOR_SESSION is None:
        from pathlib import Path
        _PROJECT_ROOT_FOR_SESSION = Path(__file__).resolve().parents[1]
    return _PROJECT_ROOT_FOR_SESSION / "data" / "instagram_session.bin"


def _login_instaloader(L, username: str, password: str) -> bool:
    """Login with session caching.

    First call: real login → save session to disk.
    Later calls: load session from disk (avoids triggering Instagram's
    "suspicious login" rate limit).
    """
    if not username or not password:
        return False
    sp = _session_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    if sp.exists():
        try:
            L.load_session_from_file(username, str(sp))
            log.info("Instagram: reused saved session for @%s", username)
            return True
        except Exception as e:
            log.warning("Failed to load saved session, re-logging in: %s", e)
    try:
        L.login(username, password)
        L.save_session_to_file(str(sp))
        log.info("Instagram: logged in as @%s and saved session", username)
        return True
    except Exception as e:
        log.warning("Instagram login failed: %s", e)
        return False


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
    creator_profiles: Optional[list[str]] = None,
) -> list[Candidate]:
    """Find trending Reels for a niche.

    Strategy (in order):
      1. Iterate `creator_profiles` (curated handles per niche from config) and
         pull their recent Reels. This works reliably even when hashtag scraping
         is blocked, because Profile.get_posts() uses authenticated GraphQL.
      2. Hashtag scan as a fallback (often blocked by Instagram).

    Always:
      - skip corporate / verified accounts
      - skip blocklisted creators
      - reuse session file to avoid triggering rate limits
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
        max_connection_attempts=1,
        request_timeout=30,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/127.0.0.0 Safari/537.36"
        ),
    )

    logged_in = _login_instaloader(L, instagram_username or "",
                                   instagram_password or "")
    if not logged_in:
        log.info("Instagram: not logged in — skipping discovery. "
                 "Set INSTAGRAM_USERNAME / INSTAGRAM_PASSWORD secrets to enable.")
        return []

    blocked = load_blocklist()
    out: list[Candidate] = []

    def _add_post(post, niche_name: str, trusted: bool = False) -> None:
        if not post.is_video:
            return
        duration = getattr(post, "video_duration", None) or 0
        if duration and duration > max_duration:
            return
        views = post.video_view_count or 0
        likes = post.likes or 0
        # Trusted (curated) profiles bypass view/like thresholds since the user
        # explicitly chose them as quality sources.
        if not trusted:
            if views and views < min_views:
                return
            if views and likes / max(views, 1) < min_like_ratio:
                return
        owner = (post.owner_username or "").lower().lstrip("@")
        if not owner:
            return
        if not trusted:
            try:
                is_verified = bool(getattr(post.owner_profile, "is_verified", False))
            except Exception:
                is_verified = False
            if exclude_verified and is_corporate_handle(owner, is_verified):
                return
        if owner in blocked:
            return
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
            niche=niche_name,
        ))

    # 1. Profile-based discovery (preferred, more reliable)
    if creator_profiles:
        profiles = list(dict.fromkeys(p.strip().lstrip("@") for p in creator_profiles
                                       if p and p.strip()))
        random.shuffle(profiles)
        for handle in profiles:
            if len(out) >= max_results:
                break
            try:
                profile = instaloader.Profile.from_username(L.context, handle)
                log.info("Scanning @%s for Reels", handle)
                checked = 0
                for post in profile.get_posts():
                    if checked >= 12 or len(out) >= max_results:
                        break
                    checked += 1
                    try:
                        _add_post(post, niche, trusted=True)
                    except Exception as e:
                        log.debug("Skipping post by @%s: %s", handle, e)
            except Exception as e:
                log.warning("Profile @%s failed: %s", handle, e)

    # Note: Instagram's hashtag GraphQL endpoint now requires a "trusted"
    # session (aged account, normal usage history). Anonymous and new-account
    # logins always get login_required. We've removed the hashtag fallback
    # because it's a guaranteed dead-end and just slows the pipeline.
    # If your curated creator profiles all fail too, your IG account is likely
    # too new — use it on Instagram.com normally for a few days first.

    log.info("Instagram discovery: %d candidates for niche=%s", len(out), niche)
    return out


# ============================================================
# TikTok (via TikWM public API — no auth, watermark-free)
# ============================================================
# TikWM is a free public service that wraps TikTok's content. It returns
# direct MP4 URLs with NO watermark (the `play` field) which is exactly what
# we need for repurposing. There's a soft rate limit (~1 req/s, ~120 req/min).
# If TikWM ever goes down, we fail gracefully and the pipeline still works
# from Pexels/Pixabay.

_TIKWM_BASE = "https://www.tikwm.com/api"
_TIKWM_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/127.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Referer": "https://www.tikwm.com/",
}


def _tikwm_search_hashtag(tag: str, *, count: int = 20) -> list[dict]:
    """Hit TikWM's `/feed/list` endpoint for a hashtag. Returns raw items."""
    try:
        r = requests.get(
            f"{_TIKWM_BASE}/feed/list",
            params={"type": "hashtag", "q": tag, "hd": 1, "count": count},
            headers=_TIKWM_HEADERS,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.debug("TikWM hashtag %s failed: %s", tag, e)
        return []
    if not isinstance(data, dict) or data.get("code") not in (0, "0"):
        log.debug("TikWM hashtag %s returned non-zero code: %s", tag,
                  (data or {}).get("msg") if isinstance(data, dict) else data)
        return []
    payload = data.get("data") or {}
    if isinstance(payload, list):
        return payload
    return payload.get("videos") or payload.get("list") or []


def _tikwm_resolve_video(video_url: str) -> Optional[dict]:
    """Resolve a single TikTok URL → TikWM metadata (with no-wm play URL)."""
    try:
        r = requests.post(
            f"{_TIKWM_BASE}/",
            data={"url": video_url, "hd": 1},
            headers=_TIKWM_HEADERS,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.debug("TikWM resolve %s failed: %s", video_url, e)
        return None
    if not isinstance(data, dict) or data.get("code") not in (0, "0"):
        return None
    return data.get("data") or None


def discover_tiktok(
    keywords: list[str],
    niche: str,
    *,
    max_results: int = 8,
    max_duration: float = 60.0,
    min_duration: float = 6.0,
    min_views: int = 50_000,
    candidates_per_tag: int = 20,
    concept: Optional[str] = None,
) -> list[Candidate]:
    """Find trending TikTok videos via TikWM public API (watermark-free)."""
    blocked = load_blocklist()
    out: list[Candidate] = []
    seen_ids: set[str] = set()
    queries = list(keywords)
    random.shuffle(queries)

    for kw in queries:
        if len(out) >= max_results:
            break
        tag = re.sub(r"[^a-z0-9_]", "", kw.lower())
        if not tag:
            continue
        log.info("TikWM hashtag: #%s", tag)
        items = _tikwm_search_hashtag(tag, count=candidates_per_tag)
        if not items:
            continue
        for v in items:
            if len(out) >= max_results:
                break
            try:
                vid_id = str(v.get("video_id") or v.get("id") or "")
                if not vid_id or vid_id in seen_ids:
                    continue
                duration = float(v.get("duration") or 0)
                if duration and (duration < min_duration
                                 or duration > max_duration):
                    continue
                views = int(v.get("play_count") or 0)
                if views and views < min_views:
                    continue
                # Prefer HD play URL, fall back to standard play. Both are
                # watermark-free; `wmplay` is the watermarked one (we skip it).
                play_url = (v.get("hdplay") or v.get("play") or "").strip()
                if not play_url:
                    continue
                author = v.get("author") or {}
                uploader = (author.get("unique_id") or "").lstrip("@").lower()
                if uploader and uploader in blocked:
                    continue
                title = (v.get("title") or v.get("desc") or "")[:500]
                tt_url = (f"https://www.tiktok.com/@{uploader}/video/{vid_id}"
                          if uploader else play_url)
                seen_ids.add(vid_id)
                out.append(Candidate(
                    source_type="tiktok",
                    source_url=tt_url,
                    download_url=play_url,
                    creator_handle=uploader or None,
                    creator_url=(f"https://www.tiktok.com/@{uploader}"
                                 if uploader else None),
                    caption=title,
                    hashtags=f"#{tag}",
                    duration_sec=duration or None,
                    niche=niche,
                    concept=concept or kw,
                ))
            except Exception as e:
                log.debug("Skip TikWM item: %s", e)
                continue

    log.info("TikTok (TikWM) discovery: %d candidates for niche=%s",
             len(out), niche)
    return out


# ============================================================
# Pexels (free, copyright-free)
# ============================================================

def _select_best_pexels_file(files: list[dict]) -> Optional[dict]:
    """Pick the highest-quality vertical/portrait file (>=1080p preferred)."""
    if not files:
        return None
    # Prefer portrait (h > w) at 1080p+ first; fallback to any 1080p+; then any
    portrait_hd = []
    any_hd = []
    for f in files:
        w = f.get("width") or 0
        h = f.get("height") or 0
        link = f.get("link") or ""
        if not link or "mp4" not in link.lower() and ".mp4" not in link.lower():
            # Pexels uses .mp4 URLs even without explicit hint
            pass
        if h >= 1080:
            any_hd.append(f)
            if h > w:  # portrait
                portrait_hd.append(f)
    pool = portrait_hd or any_hd or files
    # Sort by resolution descending
    pool.sort(key=lambda f: (f.get("height") or 0) * (f.get("width") or 0),
              reverse=True)
    return pool[0]


def discover_pexels(
    keywords: list[str],
    niche: str,
    api_key: str,
    *,
    per_page: int = 15,
    max_results: int = 8,
    min_duration: float = 8.0,
    max_duration: float = 60.0,
    concept: Optional[str] = None,
) -> list[Candidate]:
    """Search Pexels Video API with multiple keywords, prefer portrait HD."""
    if not api_key:
        return []
    out: list[Candidate] = []
    seen_urls: set[str] = set()
    headers = {"Authorization": api_key}
    queries = list(keywords)
    random.shuffle(queries)

    for kw in queries:
        if len(out) >= max_results:
            break
        try:
            r = requests.get(
                "https://api.pexels.com/videos/search",
                headers=headers,
                params={
                    "query": kw,
                    "per_page": per_page,
                    "orientation": "portrait",
                    "size": "large",   # Pexels: large = HD+
                },
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
            duration = float(v.get("duration") or 0)
            if duration < min_duration or duration > max_duration:
                continue
            best = _select_best_pexels_file(v.get("video_files", []))
            if not best:
                continue
            link = best["link"]
            if link in seen_urls:
                continue
            seen_urls.add(link)
            out.append(Candidate(
                source_type="pexels",
                source_url=link,
                creator_handle=v.get("user", {}).get("name"),
                creator_url=v.get("user", {}).get("url"),
                caption=(f"Pexels video — {kw} — by "
                         f"{v.get('user', {}).get('name', 'unknown')}"),
                hashtags=f"#{kw.replace(' ', '')}",
                duration_sec=duration,
                niche=niche,
                concept=concept or kw,
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
    per_page: int = 20,
    max_results: int = 8,
    min_duration: float = 8.0,
    max_duration: float = 60.0,
    concept: Optional[str] = None,
) -> list[Candidate]:
    """Search Pixabay Video API. Prefer large (1080p+), portrait, popular."""
    if not api_key:
        return []
    out: list[Candidate] = []
    seen_urls: set[str] = set()
    queries = list(keywords)
    random.shuffle(queries)

    for kw in queries:
        if len(out) >= max_results:
            break
        try:
            r = requests.get(
                "https://pixabay.com/api/videos/",
                params={
                    "key": api_key,
                    "q": kw,
                    "per_page": per_page,
                    "safesearch": "true",
                    "order": "popular",      # most-viewed first
                    "video_type": "all",
                    "min_width": 720,
                },
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
            duration = float(v.get("duration") or 0)
            if duration < min_duration or duration > max_duration:
                continue
            videos = v.get("videos", {})
            # Prefer largest portrait variant; Pixabay's "large" is usually 1920p
            best = None
            for size in ("large", "medium", "small"):
                cand = videos.get(size)
                if cand and cand.get("url"):
                    w = cand.get("width") or 0
                    h = cand.get("height") or 0
                    if h >= 1080:
                        best = cand
                        break
            if not best:
                best = videos.get("large") or videos.get("medium") or videos.get("small")
            if not best or not best.get("url"):
                continue
            url = best["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            out.append(Candidate(
                source_type="pixabay",
                source_url=url,
                creator_handle=v.get("user"),
                creator_url=f"https://pixabay.com/users/"
                            f"{v.get('user', '')}-{v.get('user_id', '')}/",
                caption=f"Pixabay video — {kw} — by {v.get('user', 'unknown')}",
                hashtags=" ".join(f"#{t.strip()}" for t in
                                   (v.get("tags") or "").split(",")[:8]),
                duration_sec=duration,
                niche=niche,
                concept=concept or kw,
            ))
    log.info("Pixabay discovery: %d candidates for niche=%s", len(out), niche)
    return out


# ============================================================
# Photos (Pexels + Pixabay) — converted to Ken-Burns video clips
# ============================================================

def discover_pexels_photos(
    keywords: list[str],
    niche: str,
    api_key: str,
    *,
    per_page: int = 15,
    max_results: int = 6,
    concept: Optional[str] = None,
) -> list[Candidate]:
    """Search Pexels Photos. Each result becomes an `image` candidate that
    the editor will animate with Ken Burns zoom into a cinematic clip.
    """
    if not api_key:
        return []
    out: list[Candidate] = []
    seen: set[str] = set()
    headers = {"Authorization": api_key}
    queries = list(keywords)
    random.shuffle(queries)
    for kw in queries:
        if len(out) >= max_results:
            break
        try:
            r = requests.get(
                "https://api.pexels.com/v1/search",
                headers=headers,
                params={
                    "query": kw,
                    "per_page": per_page,
                    "orientation": "portrait",
                    "size": "large",
                },
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("Pexels-photos fetch failed for %s: %s", kw, e)
            continue
        for p in data.get("photos", []):
            if len(out) >= max_results:
                break
            src = p.get("src") or {}
            # Prefer the highest-resolution available
            url = (src.get("original") or src.get("large2x")
                   or src.get("large") or src.get("medium"))
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(Candidate(
                source_type="pexels",
                source_url=p.get("url") or url,
                download_url=url,
                creator_handle=(p.get("photographer") or "").strip() or None,
                creator_url=p.get("photographer_url"),
                caption=f"Pexels photo — {kw} — by "
                        f"{p.get('photographer', 'unknown')}",
                hashtags=f"#{kw.replace(' ', '')}",
                duration_sec=None,
                niche=niche,
                media_kind="image",
                concept=concept or kw,
            ))
    log.info("Pexels-photos discovery: %d candidates for niche=%s",
             len(out), niche)
    return out


def discover_pixabay_photos(
    keywords: list[str],
    niche: str,
    api_key: str,
    *,
    per_page: int = 20,
    max_results: int = 6,
    concept: Optional[str] = None,
) -> list[Candidate]:
    """Search Pixabay Photos. Returns image candidates."""
    if not api_key:
        return []
    out: list[Candidate] = []
    seen: set[str] = set()
    queries = list(keywords)
    random.shuffle(queries)
    for kw in queries:
        if len(out) >= max_results:
            break
        try:
            r = requests.get(
                "https://pixabay.com/api/",
                params={
                    "key": api_key,
                    "q": kw,
                    "per_page": per_page,
                    "safesearch": "true",
                    "image_type": "photo",
                    "orientation": "vertical",
                    "order": "popular",
                    "min_width": 1080,
                },
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("Pixabay-photos fetch failed for %s: %s", kw, e)
            continue
        for p in data.get("hits", []):
            if len(out) >= max_results:
                break
            url = (p.get("largeImageURL") or p.get("webformatURL"))
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(Candidate(
                source_type="pixabay",
                source_url=p.get("pageURL") or url,
                download_url=url,
                creator_handle=p.get("user"),
                creator_url=f"https://pixabay.com/users/"
                            f"{p.get('user', '')}-{p.get('user_id', '')}/",
                caption=f"Pixabay photo — {kw} — by {p.get('user', 'unknown')}",
                hashtags=" ".join(f"#{t.strip()}" for t in
                                   (p.get("tags") or "").split(",")[:8]),
                duration_sec=None,
                niche=niche,
                media_kind="image",
                concept=concept or kw,
            ))
    log.info("Pixabay-photos discovery: %d candidates for niche=%s",
             len(out), niche)
    return out


# ============================================================
# Unsplash photos — high-quality royalty-free images, no watermark
# https://unsplash.com/documentation
# ============================================================

def discover_unsplash_photos(
    keywords: list[str],
    niche: str,
    access_key: str,
    *,
    per_page: int = 15,
    max_results: int = 6,
    concept: Optional[str] = None,
) -> list[Candidate]:
    """Search Unsplash for portrait, high-resolution photos.

    Each result is an `image` candidate that the editor will animate
    with Ken Burns zoom. Unsplash images are royalty-free and watermark-free.
    """
    if not access_key:
        return []
    out: list[Candidate] = []
    seen: set[str] = set()
    headers = {
        "Accept-Version": "v1",
        "Authorization": f"Client-ID {access_key}",
    }
    queries = list(keywords)
    random.shuffle(queries)
    for kw in queries:
        if len(out) >= max_results:
            break
        try:
            r = requests.get(
                "https://api.unsplash.com/search/photos",
                headers=headers,
                params={
                    "query": kw,
                    "per_page": per_page,
                    "orientation": "portrait",
                    "content_filter": "high",
                },
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("Unsplash fetch failed for %s: %s", kw, e)
            continue
        for p in data.get("results", []):
            if len(out) >= max_results:
                break
            urls = p.get("urls") or {}
            # Prefer the highest-quality non-raw URL (raw needs format params).
            url = (urls.get("full") or urls.get("regular")
                   or urls.get("small"))
            if not url or url in seen:
                continue
            # Per Unsplash API guidelines we should hit the download endpoint
            # to register the download. Fire-and-forget; failures are fine.
            dl_link = (p.get("links") or {}).get("download_location")
            if dl_link:
                try:
                    requests.get(dl_link, headers=headers, timeout=8)
                except Exception:
                    pass
            seen.add(url)
            user = (p.get("user") or {})
            handle = user.get("username") or user.get("name")
            user_link = ((user.get("links") or {}).get("html")
                         or f"https://unsplash.com/@{handle}" if handle else None)
            out.append(Candidate(
                source_type="unsplash",
                source_url=(p.get("links") or {}).get("html") or url,
                download_url=url,
                creator_handle=handle,
                creator_url=user_link,
                caption=f"Unsplash photo — {kw} — by "
                        f"{user.get('name', handle or 'unknown')}",
                hashtags=" ".join(
                    f"#{t.get('title', '').replace(' ', '')}"
                    for t in (p.get("tags") or [])[:8]
                    if t.get("title")
                ),
                duration_sec=None,
                niche=niche,
                media_kind="image",
                concept=concept or kw,
            ))
    log.info("Unsplash discovery: %d candidates for niche=%s",
             len(out), niche)
    return out


# ============================================================
# Web image search — DuckDuckGo (no key, free, web-wide)
# Google CSE was removed because Google deprecated the
# "search the entire web" toggle, making CSE useless for our case.
# ============================================================

# Stock-photo sites whose previews carry watermarks. Skip them.
_WATERMARK_STOCK_DOMAINS = (
    "shutterstock", "gettyimages", "alamy", "istockphoto", "dreamstime",
    "depositphotos", "123rf", "stock.adobe", "adobestock", "agefotostock",
    "canstockphoto", "fotosearch", "bigstockphoto", "lookphotos",
)


# ============================================================
# Openverse — federated CC-licensed image search (run by WordPress).
# Aggregates Flickr / Wikimedia / museums / 50+ open sources.
# No API key required. https://api.openverse.org/v1/
# This replaces DuckDuckGo (which started 403'ing scrapers in 2024).
# ============================================================

def discover_openverse_images(
    keywords: list[str],
    niche: str,
    *,
    max_results: int = 6,
    concept: Optional[str] = None,
) -> list[Candidate]:
    """Openverse image search — no key, only CC-licensed results.

    All results are royalty-free and watermark-free by API guarantee
    (Openverse only indexes CC0/CC-BY/PDM/etc sources).
    """
    out: list[Candidate] = []
    seen: set[str] = set()
    queries = list(keywords)
    random.shuffle(queries)
    headers = {
        "User-Agent": ("AutoReelsToYouTube/1.0 "
                       "(+https://github.com/yourname/autoreels)"),
        "Accept": "application/json",
    }
    # Openverse accepts comma-separated multi-query, but per-query gives
    # us better relevance ranking, so loop.
    for kw in queries:
        if len(out) >= max_results:
            break
        try:
            r = requests.get(
                "https://api.openverse.org/v1/images/",
                headers=headers,
                params={
                    "q": kw,
                    "page_size": 20,
                    "size": "large",
                    "license_type": "all-cc",   # all CC + PDM
                    "mature": "false",
                    "aspect_ratio": "tall",     # prefer portrait/vertical
                },
                timeout=20,
            )
            if r.status_code == 429:
                log.warning("Openverse rate-limited, skipping rest")
                break
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("Openverse fetch failed for %s: %s", kw, e)
            continue
        for item in data.get("results", []):
            if len(out) >= max_results:
                break
            url = item.get("url") or item.get("thumbnail")
            page = (item.get("foreign_landing_url")
                    or item.get("related_url") or url)
            host = ""
            if url:
                m = re.search(r"https?://([^/]+)/", url + "/")
                host = (m.group(1) if m else "").lower()
            width = item.get("width") or 0
            if not url or url in seen:
                continue
            if any(d in (host + (url or "")).lower()
                   for d in _WATERMARK_STOCK_DOMAINS):
                continue
            if width and width < 600:
                continue
            seen.add(url)
            creator = item.get("creator") or host or "unknown"
            license_name = item.get("license") or "cc"
            out.append(Candidate(
                source_type="openverse",
                source_url=page,
                download_url=url,
                creator_handle=item.get("creator"),
                creator_url=item.get("creator_url") or page,
                caption=f"Openverse photo — {kw} — by {creator} ({license_name})",
                hashtags=f"#{kw.replace(' ', '')}",
                duration_sec=None,
                niche=niche,
                media_kind="image",
                concept=concept or kw,
            ))
    log.info("Openverse discovery: %d candidates for niche=%s",
             len(out), niche)
    return out
