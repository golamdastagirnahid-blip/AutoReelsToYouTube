"""Pipeline orchestrator — runs end-to-end for one video at a time.

Modes:
  produce  — discover → download → analyze → script → vo → edit (no upload)
  upload   — pick the next finished, due video and upload to YouTube
  full     — produce N then upload due

Usage:
  python -m src.main produce --niche motivation --count 1
  python -m src.main upload
  python -m src.main full --count 1
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from src.analyzer import analyze_video
from src.captions import generate_captions
from src.discovery import (Candidate, discover_instagram,
                           discover_openverse_images, discover_pexels,
                           discover_pexels_photos, discover_pixabay,
                           discover_pixabay_photos, discover_tiktok,
                           discover_unsplash_photos)
from src.downloader import download, download_to, hash_file
from src.editor import edit_video, edit_video_multiclip
from src.script_writer import extract_visual_concepts, write_script
from src.scheduler import humanized_slots
from src.uploader import upload_short
from src.utils.db import Tracker
from src.utils.secrets import Secrets
from src.voiceover import audio_duration, fit_audio_to_duration, synthesize

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("autoreels")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = _PROJECT_ROOT / "config.yaml"


# ============================================================
# Helpers
# ============================================================

def _utc_now() -> datetime:
    """Naive UTC `datetime` (matches what SQLite stores).

    Replaces deprecated `datetime.utcnow()` while preserving the existing
    naive-datetime contract used throughout the tracker.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def pick_niche(cfg: dict, requested: Optional[str],
               tracker: Optional[Tracker] = None) -> dict:
    """Pick the next niche.

    Priority:
      1. Explicit --niche flag (always wins).
      2. Round-robin: rotate to the niche AFTER the last used one.
         Persisted in tracker.kv_state so it survives across runs.
      3. Random fallback if no state.
    """
    niches = cfg["niches"]
    if not niches:
        raise SystemExit("No niches configured")

    if requested:
        for n in niches:
            if n["name"] == requested:
                return n
        raise SystemExit(f"Unknown niche: {requested}. "
                         f"Available: {[n['name'] for n in niches]}")

    if tracker is not None:
        last = tracker.kv_get("last_niche")
        names = [n["name"] for n in niches]
        if last and last in names:
            idx = (names.index(last) + 1) % len(names)
        else:
            idx = 0
        chosen = niches[idx]
        tracker.kv_set("last_niche", chosen["name"])
        log.info("Niche rotation: %s (after last=%s)",
                 chosen["name"], last or "<none>")
        return chosen

    return random.choice(niches)


def discover_for(niche: dict, cfg: dict, secrets: Secrets) -> list[Candidate]:
    out: list[Candidate] = []
    src_cfg = cfg["sources"]
    if src_cfg.get("instagram", {}).get("enabled"):
        ig = src_cfg["instagram"]
        out += discover_instagram(
            keywords=niche["keywords"],
            niche=niche["name"],
            min_views=ig.get("min_views", 100_000),
            min_like_ratio=ig.get("min_like_ratio", 0.03),
            max_duration=cfg["filters"]["max_duration_sec"],
            exclude_verified=ig.get("exclude_verified", True),
            instagram_username=secrets.instagram_username,
            instagram_password=secrets.instagram_password,
            creator_profiles=niche.get("creator_profiles", []),
            max_results=8,
        )
    if src_cfg.get("tiktok", {}).get("enabled", True):
        tk = src_cfg.get("tiktok", {})
        # Use the niche-level tiktok_keywords if present (more topical),
        # otherwise fall back to the visual keywords.
        tk_keywords = niche.get("tiktok_keywords") or niche["keywords"]
        out += discover_tiktok(
            keywords=tk_keywords,
            niche=niche["name"],
            max_results=tk.get("max_results", 8),
            min_views=tk.get("min_views", 50_000),
            max_duration=cfg["filters"]["max_duration_sec"],
            min_duration=cfg["filters"].get("min_duration_sec", 6),
        )
    if src_cfg.get("copyright_free", {}).get("enabled"):
        quota = src_cfg["copyright_free"].get("per_run_quota", 3)
        if "pexels" in src_cfg["copyright_free"]["providers"] and secrets.pexels_api_key:
            out += discover_pexels(niche["keywords"], niche["name"],
                                   secrets.pexels_api_key, max_results=quota)
        if "pixabay" in src_cfg["copyright_free"]["providers"] and secrets.pixabay_api_key:
            out += discover_pixabay(niche["keywords"], niche["name"],
                                    secrets.pixabay_api_key, max_results=quota)
    random.shuffle(out)
    return out


# ============================================================
# Pipeline stages (per-video)
# ============================================================

def _download_broll(extras: list[Candidate], primary_vid: int,
                    want: int) -> list[Path]:
    """Try to download up to `want` extra B-roll clips from `extras`.

    Each extra clip lands in data/downloads/<primary_vid>_broll<N>.mp4.
    Failed downloads are skipped silently. Returns successfully downloaded paths.
    """
    from src.downloader import DOWNLOAD_DIR
    out: list[Path] = []
    for cand in extras:
        if len(out) >= want:
            break
        target = DOWNLOAD_DIR / f"{primary_vid}_broll{len(out):02d}.mp4"
        # For TikTok we have a direct watermark-free MP4 in download_url;
        # for images, we need to take the Ken-Burns conversion path. The
        # download_to helper handles all of that based on media_kind.
        url = cand.fetch_url
        p = download_to(url, target,
                        source_type=cand.source_type,
                        media_kind=cand.media_kind)
        if p and p.exists() and p.stat().st_size > 10_000:
            out.append(p)
        else:
            log.debug("B-roll download failed: %s", url)
    return out


# ============================================================
# Multi-angle script-targeted discovery
# ============================================================

def _expand_concept(c: dict) -> list[str]:
    """Return the concept's primary query plus any alt phrasings, deduped."""
    query = (c.get("query") or "").strip()
    alts = [str(a).strip() for a in (c.get("alt") or []) if a]
    seen: set[str] = set()
    out: list[str] = []
    for q in [query] + alts:
        ql = q.lower()
        if ql and ql not in seen:
            seen.add(ql)
            out.append(q)
    return out


def discover_for_script(
    visual_concepts: list[dict],
    niche: str,
    cfg: dict,
    secrets: Secrets,
    *,
    per_concept_videos: int = 2,
    per_concept_photos: int = 1,
    include_tiktok: bool = True,
) -> list[Candidate]:
    """For each visual concept, search every source (multi-angle).

    Returns candidates ordered by concept (so the first concept's clips appear
    first in the multi-clip composition — which lines up with the script's
    narrative order).
    """
    src_cfg = cfg.get("sources", {}) or {}
    out: list[Candidate] = []
    seen_urls: set[str] = set()

    def _push(items: list[Candidate]) -> None:
        for it in items:
            key = it.fetch_url
            if key and key not in seen_urls:
                seen_urls.add(key)
                out.append(it)

    for concept in visual_concepts:
        variants = _expand_concept(concept)
        if not variants:
            continue
        primary = concept.get("query") or variants[0]

        # 1. Pexels videos
        if (src_cfg.get("copyright_free", {}).get("enabled")
                and "pexels" in src_cfg["copyright_free"].get("providers", [])
                and secrets.pexels_api_key):
            _push(discover_pexels(
                variants, niche, secrets.pexels_api_key,
                max_results=per_concept_videos, concept=primary,
            ))

        # 2. Pixabay videos
        if (src_cfg.get("copyright_free", {}).get("enabled")
                and "pixabay" in src_cfg["copyright_free"].get("providers", [])
                and secrets.pixabay_api_key):
            _push(discover_pixabay(
                variants, niche, secrets.pixabay_api_key,
                max_results=per_concept_videos, concept=primary,
            ))

        # 3. TikTok (skip per-concept by default — TikTok hashtags don't always
        #    map cleanly to free-form visual phrases). Caller can opt in.
        if include_tiktok and src_cfg.get("tiktok", {}).get("enabled", True):
            tk = src_cfg.get("tiktok", {})
            _push(discover_tiktok(
                variants, niche,
                max_results=per_concept_videos,
                min_views=tk.get("min_views", 50_000),
                max_duration=cfg["filters"]["max_duration_sec"],
                min_duration=cfg["filters"].get("min_duration_sec", 6),
                concept=primary,
            ))

        # 4. Photos as enrichment (Ken-Burns clip in editor).
        #    Order: curated royalty-free first (Unsplash/Pexels/Pixabay),
        #    then DDG for entire-web breadth on niche-specific concepts.
        if per_concept_photos > 0:
            if secrets.unsplash_access_key:
                _push(discover_unsplash_photos(
                    variants, niche, secrets.unsplash_access_key,
                    max_results=per_concept_photos, concept=primary,
                ))
            if secrets.pexels_api_key:
                _push(discover_pexels_photos(
                    variants, niche, secrets.pexels_api_key,
                    max_results=per_concept_photos, concept=primary,
                ))
            if secrets.pixabay_api_key:
                _push(discover_pixabay_photos(
                    variants, niche, secrets.pixabay_api_key,
                    max_results=per_concept_photos, concept=primary,
                ))
            # Openverse: federated CC search (Flickr / Wikimedia / museums).
            # No key required, all results are watermark-free CC-licensed.
            _push(discover_openverse_images(
                variants, niche,
                max_results=per_concept_photos, concept=primary,
            ))

    log.info("Script-targeted discovery: %d candidates across %d concepts",
             len(out), len(visual_concepts))
    return out


def produce_one(cfg: dict, secrets: Secrets, tracker: Tracker,
                niche_name: Optional[str]) -> Optional[int]:
    niche = pick_niche(cfg, niche_name, tracker=tracker)
    log.info("Niche: %s", niche["name"])

    candidates = discover_for(niche, cfg, secrets)
    log.info("Discovered %d candidates", len(candidates))

    multi_cfg = cfg.get("editing", {}).get("multiclip", {}) or {}
    multiclip_enabled = bool(multi_cfg.get("enabled", True))
    target_clips = int(multi_cfg.get("target_clips", 5))
    min_multi = int(multi_cfg.get("min_clips_for_multi", 2))
    fade_duration = float(multi_cfg.get("fade_duration_sec", 0.4))

    for cand_idx, cand in enumerate(candidates):
        if tracker.has_url(cand.source_url):
            continue
        if cand.creator_handle and tracker.is_blocked(cand.creator_handle):
            continue

        vid = tracker.insert_discovery(
            source_type=cand.source_type,
            source_url=cand.source_url,
            creator_handle=cand.creator_handle,
            creator_url=cand.creator_url,
            caption=cand.caption,
            hashtags=cand.hashtags,
            niche=niche["name"],
        )
        log.info("Working on video #%d (%s)", vid, cand.source_url)

        try:
            # 1. Download primary clip
            local = download(cand, vid)
            if not local:
                tracker.update(vid, status="failed", error="download failed")
                continue
            file_hash = hash_file(local)
            if tracker.has_hash(file_hash):
                log.info("Duplicate hash; skipping.")
                tracker.update(vid, status="skipped", error="duplicate hash",
                               file_hash=file_hash)
                continue
            tracker.update(vid, status="downloaded", local_path=str(local),
                           file_hash=file_hash)

            # 2. Analyze primary (decides script content + safety)
            analysis = analyze_video(local, cand.caption or "", secrets.nvidia_api_key,
                                     model=cfg["ai"]["model"],
                                     fallback_model=cfg["ai"]["fallback_model"])
            if cfg.get("filters", {}).get("skip_if_contains_brand_logos") \
                    or cfg.get("filters", {}).get("skip_if_contains_celebrities"):
                if analysis.get("safe_to_repurpose") is False:
                    reasons = ", ".join(analysis.get("reasons_unsafe", []))
                    log.warning("Skipping unsafe video: %s", reasons)
                    tracker.update(vid, status="skipped",
                                   error=f"unsafe: {reasons}",
                                   analysis_json=json.dumps(analysis))
                    continue
            tracker.update(vid, status="analyzed",
                           analysis_json=json.dumps(analysis),
                           duration_sec=analysis.get("duration_sec"))

            # 3. Script
            duration = float(analysis.get("duration_sec") or 30)
            script_obj = write_script(
                analysis=analysis,
                duration_sec=duration,
                niche=niche["name"],
                tone=niche["tone"],
                nvidia_api_key=secrets.nvidia_api_key,
                model=cfg["ai"]["fallback_model"],
                style=cfg["script"]["style"],
            )
            tracker.update(vid, status="scripted",
                           script_text=json.dumps(script_obj))

            # 4. Voiceover
            #    - tracker enables 24h backoff for backends that fail repeatedly
            #    - config-driven backend_order lets you reorder/disable engines
            vo = synthesize(script_obj["script"], vid,
                            api_key=secrets.elevenlabs_api_key,
                            voice_id=secrets.elevenlabs_voice_id,
                            model=cfg["voiceover"]["model"],
                            stability=cfg["voiceover"]["stability"],
                            similarity_boost=cfg["voiceover"]["similarity_boost"],
                            style=cfg["voiceover"]["style"],
                            speaker_boost=cfg["voiceover"]["speaker_boost"],
                            backend_order=cfg["voiceover"].get("backend_order"),
                            tracker=tracker)
            if not vo:
                tracker.update(vid, status="failed", error="voiceover failed")
                continue
            vo = fit_audio_to_duration(vo, duration)
            tracker.update(vid, status="voiced", voiceover_path=str(vo))

            # Actual VO duration drives video length for tight A/V sync
            vo_duration = audio_duration(vo) or duration

            # 5. Captions
            edit_cfg = cfg["editing"]
            res = tuple(int(x) for x in edit_cfg["resolution"].split("x"))
            captions_path = None
            cap_cfg = edit_cfg.get("captions", {})
            if cap_cfg.get("enabled", True):
                captions_path = generate_captions(
                    vo, vid,
                    video_width=res[0], video_height=res[1],
                    style=cap_cfg,
                )

            # 6. Gather B-roll clips
            #    Strategy:
            #      a. Ask the LLM for 5-7 visual concepts derived from the
            #         actual script narration.
            #      b. Search every source per concept (multi-angle).
            #      c. Use script-targeted clips FIRST so visuals match what
            #         the narrator is saying, falling back to the generic
            #         niche pool if we still need more.
            fonts_dir = _PROJECT_ROOT / "data" / "fonts"
            final: Optional[Path] = None

            if multiclip_enabled and target_clips > 1:
                visual_concepts = extract_visual_concepts(
                    script_obj.get("script", ""),
                    niche["name"],
                    secrets.nvidia_api_key,
                    model=cfg["ai"]["fallback_model"],
                    fallback=niche.get("keywords"),
                )
                log.info("Visual concepts (%d): %s", len(visual_concepts),
                         [c.get("query") for c in visual_concepts])

                script_pool = discover_for_script(
                    visual_concepts, niche["name"], cfg, secrets,
                    per_concept_videos=2,
                    per_concept_photos=1,
                    include_tiktok=False,  # tikwm hashtag search ≠ free-form
                )

                # Build dedup key set to avoid re-using the primary clip
                used_keys = {cand.fetch_url, cand.source_url}

                def _is_blocked(c: Candidate) -> bool:
                    return bool(c.creator_handle
                                and tracker.is_blocked(c.creator_handle))

                # Script-targeted clips FIRST, generic niche clips as fallback
                extras_pool = [c for c in script_pool
                               if c.fetch_url not in used_keys
                               and not _is_blocked(c)]
                # Then top up from the generic discovery pool
                for c in candidates:
                    if c.fetch_url in used_keys or _is_blocked(c):
                        continue
                    if c.fetch_url in {x.fetch_url for x in extras_pool}:
                        continue
                    extras_pool.append(c)

                want = max(0, target_clips - 1)
                broll = _download_broll(extras_pool, vid, want) if want else []
                all_clips = [local] + broll
                log.info("Multi-clip pool: %d clips (1 primary + %d b-roll, "
                         "%d script-targeted candidates)",
                         len(all_clips), len(broll), len(script_pool))

                if len(all_clips) >= min_multi:
                    sfx_cfg = edit_cfg.get("transition_sfx", {})
                    final = edit_video_multiclip(
                        source_videos=all_clips,
                        voiceover_audio=vo,
                        video_id=vid,
                        niche=niche["name"],
                        # short overlay text (≤4 words / ≤14 chars)
                        hook_text=script_obj.get("hook_overlay", ""),
                        target_resolution=res,
                        saturation=edit_cfg["filters"]["saturation"],
                        sharpen=edit_cfg["filters"]["sharpen"],
                        hdr_look=edit_cfg["filters"]["hdr_look"],
                        music_volume_db=edit_cfg["background_music"]["volume_db"],
                        captions_ass=captions_path,
                        fonts_dir=fonts_dir,
                        fade_duration=fade_duration,
                        target_total_duration=vo_duration,
                        sfx_enabled=sfx_cfg.get("enabled", True),
                        sfx_volume_db=sfx_cfg.get("volume_db", -8.0),
                    )

            if not final:
                # Fallback: single-clip render
                final = edit_video(
                    source_video=local,
                    voiceover_audio=vo,
                    video_id=vid,
                    niche=niche["name"],
                    hook_text=script_obj.get("hook_overlay", ""),
                    target_resolution=res,
                    saturation=edit_cfg["filters"]["saturation"],
                    sharpen=edit_cfg["filters"]["sharpen"],
                    hdr_look=edit_cfg["filters"]["hdr_look"],
                    music_volume_db=edit_cfg["background_music"]["volume_db"],
                    captions_ass=captions_path,
                    fonts_dir=fonts_dir,
                )

            if not final:
                tracker.update(vid, status="failed", error="edit failed")
                continue
            tracker.update(vid, status="edited", edited_path=str(final))
            log.info("Produced video #%d → %s", vid, final)
            return vid
        except Exception as e:
            log.exception("Pipeline error on video #%d", vid)
            tracker.update(vid, status="failed", error=str(e)[:500])
            continue

    log.warning("No produceable candidates this run.")
    return None


# ============================================================
# Upload
# ============================================================

def schedule_due(cfg: dict, tracker: Tracker) -> None:
    """If no slots are scheduled today, generate them for any 'edited' videos."""
    today = _utc_now()
    edited = tracker.by_status("edited", limit=cfg["upload"]["videos_per_day"])
    if not edited:
        return
    slots = humanized_slots(
        day=today,
        videos_per_day=len(edited),
        window_start_hour=cfg["upload"]["window_start_hour"],
        window_end_hour=cfg["upload"]["window_end_hour"],
        jitter_minutes=cfg["upload"]["jitter_minutes"],
    )
    for video, when in zip(edited, slots):
        tracker.add_schedule(int(video["id"]), when)
        log.info("Scheduled video #%d at %s", video["id"], when.isoformat())


def upload_due(cfg: dict, secrets: Secrets, tracker: Tracker) -> int:
    if cfg["safety"].get("dry_run", True):
        log.warning("dry_run=true — skipping uploads. Set dry_run: false in config.yaml")
        return 0
    if not secrets.youtube_refresh_token:
        log.error("YOUTUBE_REFRESH_TOKEN missing")
        return 0

    posted = 0
    due = tracker.due_uploads(_utc_now())
    for row in due:
        vid = int(row["id"])
        edited_path = row["edited_path"]
        if not edited_path or not Path(edited_path).exists():
            log.warning("Video #%d edited file missing", vid)
            continue
        script_obj = json.loads(row["script_text"] or "{}")

        creator = row["creator_handle"] or "unknown"
        source_url = row["source_url"]
        niche = row["niche"] or "shorts"
        hashtags = " ".join(script_obj.get("hashtags", ["#shorts", f"#{niche}"]))
        description = cfg["upload"]["description_template"].format(
            ai_description=script_obj.get("description", ""),
            creator_handle=creator,
            source_url=source_url,
            owner_email=secrets.owner_contact_email,
            niche=niche,
            hashtags=hashtags,
        )

        yid = upload_short(
            Path(edited_path),
            title=script_obj.get("title", f"Amazing {niche} short"),
            description=description,
            tags=script_obj.get("tags", []),
            client_id=secrets.youtube_client_id,
            client_secret=secrets.youtube_client_secret,
            refresh_token=secrets.youtube_refresh_token,
        )
        if yid:
            tracker.update(vid, status="uploaded", youtube_id=yid,
                           youtube_url=f"https://youtube.com/shorts/{yid}")
            tracker.mark_posted(int(row["schedule_id"]))
            posted += 1
    return posted


# ============================================================
# CLI
# ============================================================

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="autoreels")
    parser.add_argument("mode", choices=["produce", "upload", "full"])
    parser.add_argument("--niche", default=None, help="niche name (default: random)")
    parser.add_argument("--count", type=int, default=1, help="how many to produce")
    args = parser.parse_args(argv)

    cfg = load_config()
    secrets = Secrets.load()
    log.info(secrets.summary())
    tracker = Tracker()

    if args.mode in ("produce", "full"):
        for _ in range(args.count):
            produce_one(cfg, secrets, tracker, args.niche)
        schedule_due(cfg, tracker)

    if args.mode in ("upload", "full"):
        n = upload_due(cfg, secrets, tracker)
        log.info("Uploaded %d video(s)", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
