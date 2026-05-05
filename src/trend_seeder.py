"""Reddit-based trending-topic seeder.

For a given niche we poll its configured subreddits for the top posts of
the last 24h and return their titles. Those titles are handed to the
script writer as "what people are discussing RIGHT NOW" context so
generated scripts stay tied to live conversation instead of evergreen
advice. This meaningfully raises CTR on topical/news-adjacent niches
(AI, tech, space) and adds fresh phrasing to evergreen ones (motivation,
fitness).

Design decisions:

* **Anonymous Reddit JSON endpoints** — Reddit still serves the public
  `/r/<sub>/top.json` endpoint without auth as long as we send a
  descriptive User-Agent. Rate limit: ~60 req/min per IP. Our worst
  case is one request per subreddit per niche per 6h = trivial.

* **Graceful fallback** — if Reddit returns 403/429/503 or the endpoint
  is unreachable, we return `[]` and the caller proceeds without
  trending context. The script writer already works fine without it.

* **6h per-niche cache via kv_state** — we persist the fetched titles
  + timestamp in the SQLite tracker so back-to-back produce runs don't
  hammer Reddit. Cache TTL and post count are configurable.

* **No PRAW dependency** — we keep the requirements list slim. The
  direct JSON endpoint is all we need.

Example config block (one per niche in `config.yaml`):

    trending_subreddits:
      - MachineLearning
      - OpenAI
      - singularity

And globally:

    trending:
      enabled: true
      posts_per_niche: 6
      cache_ttl_hours: 6
      user_agent: "AutoReels-Bot/1.0 (by /u/autoreels-dev)"
"""
from __future__ import annotations

import json
import logging
import random
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

_REDDIT_TOP_URL = "https://www.reddit.com/r/{sub}/top.json"

# Process-wide kill switch. Once we hit a hard block (e.g. 403 Forbidden
# from the Reddit edge), we stop calling Reddit for the rest of the run.
# Reset between pipeline invocations, which is exactly what we want.
_REDDIT_DISABLED = False


def _fetch_subreddit_top(
    subreddit: str,
    *,
    user_agent: str,
    limit: int = 10,
    timeout: int = 12,
) -> list[str]:
    """Return a list of post titles from /r/<sub>/top (last 24h)."""
    global _REDDIT_DISABLED
    if _REDDIT_DISABLED:
        return []
    try:
        r = requests.get(
            _REDDIT_TOP_URL.format(sub=subreddit),
            params={"t": "day", "limit": limit, "raw_json": 1},
            headers={"User-Agent": user_agent},
            timeout=timeout,
        )
    except Exception as e:
        log.warning("Reddit fetch error for r/%s: %s", subreddit, e)
        return []

    # Hard-block detection: Reddit returns 403 when they're rate-limiting
    # an IP and 429 on explicit quota breach. Trip the kill switch so we
    # don't keep hammering.
    if r.status_code in (403, 429):
        _REDDIT_DISABLED = True
        log.warning(
            "Reddit rate-limited r/%s (HTTP %d) — disabling Reddit trend "
            "seeding for the remainder of this run.",
            subreddit, r.status_code,
        )
        return []
    if r.status_code != 200:
        log.info("Reddit r/%s returned HTTP %d; skipping.",
                 subreddit, r.status_code)
        return []

    try:
        data = r.json()
    except Exception as e:
        log.warning("Reddit r/%s returned non-JSON body: %s", subreddit, e)
        return []

    titles: list[str] = []
    for child in (data.get("data") or {}).get("children", []) or []:
        post = child.get("data") or {}
        title = (post.get("title") or "").strip()
        if not title or post.get("stickied"):
            continue
        # Skip moderator / meta posts and low-score noise.
        if post.get("score", 0) < 50:
            continue
        titles.append(title)
    return titles


def fetch_trending_titles(
    *,
    niche_name: str,
    subreddits: list[str],
    user_agent: str,
    posts_per_niche: int = 6,
    cache_ttl_hours: int = 6,
    tracker=None,
) -> list[str]:
    """Return up to `posts_per_niche` trending titles for the niche.

    If `tracker` is provided, results are cached per-niche in kv_state
    with a `cache_ttl_hours` TTL so back-to-back pipeline runs don't
    re-poll Reddit.
    """
    if not subreddits:
        return []

    cache_key = f"trending:{niche_name}"

    # Cache read
    if tracker is not None:
        raw = tracker.kv_get(cache_key)
        if raw:
            try:
                cached = json.loads(raw)
                age_sec = time.time() - float(cached.get("fetched_at", 0))
                if age_sec < cache_ttl_hours * 3600:
                    titles = list(cached.get("titles") or [])
                    if titles:
                        log.info(
                            "Trending (cache hit, age %.1fh): %d titles "
                            "for niche=%s",
                            age_sec / 3600, len(titles), niche_name,
                        )
                        return titles[:posts_per_niche]
            except Exception as e:
                log.debug("Ignoring malformed trending cache: %s", e)

    # Cache miss → fetch fresh. Shuffle subs so we don't always hit the
    # same one first (spreads load, reduces per-sub bias).
    subs = list(subreddits)
    random.shuffle(subs)

    all_titles: list[str] = []
    per_sub_limit = max(4, posts_per_niche // max(1, len(subs)) + 3)
    for sub in subs:
        titles = _fetch_subreddit_top(
            sub, user_agent=user_agent, limit=per_sub_limit,
        )
        if titles:
            log.info("Reddit r/%s: %d trending titles", sub, len(titles))
            all_titles.extend(titles)
        if len(all_titles) >= posts_per_niche * 2:
            break  # we have enough; no need to keep polling

    # De-dupe while preserving order, cap to requested count.
    seen: set[str] = set()
    deduped: list[str] = []
    for t in all_titles:
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(t)
    final = deduped[:posts_per_niche]

    # Cache write (always — including empty results, to avoid hammering
    # a broken endpoint for the full TTL window).
    if tracker is not None:
        try:
            tracker.kv_set(cache_key, json.dumps({
                "fetched_at": time.time(),
                "titles": final,
            }))
        except Exception as e:
            log.debug("Failed to cache trending titles: %s", e)

    log.info("Trending (fresh): %d titles for niche=%s from %d subreddit(s)",
             len(final), niche_name, len(subs))
    return final


def fetch_trending_for_niche(
    cfg: dict, niche: dict, tracker=None,
) -> Optional[list[str]]:
    """High-level wrapper: read all config, return titles or None if disabled.

    Returns None (not []) when the feature is turned off in config or when
    the niche has no subreddits configured, so callers can distinguish
    "not available" from "fetched but empty".
    """
    tr_cfg = cfg.get("trending") or {}
    if not tr_cfg.get("enabled", False):
        return None
    subs = list(niche.get("trending_subreddits") or [])
    if not subs:
        return None
    return fetch_trending_titles(
        niche_name=niche.get("name", "unknown"),
        subreddits=subs,
        user_agent=tr_cfg.get(
            "user_agent", "AutoReels-Bot/1.0 (anonymous)"),
        posts_per_niche=int(tr_cfg.get("posts_per_niche", 6)),
        cache_ttl_hours=int(tr_cfg.get("cache_ttl_hours", 6)),
        tracker=tracker,
    )
