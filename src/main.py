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
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from src.analyzer import analyze_video
from src.discovery import (Candidate, discover_instagram, discover_pexels,
                           discover_pixabay)
from src.downloader import download, hash_file
from src.editor import edit_video
from src.script_writer import write_script
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

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def pick_niche(cfg: dict, requested: Optional[str]) -> dict:
    niches = cfg["niches"]
    if requested:
        for n in niches:
            if n["name"] == requested:
                return n
        raise SystemExit(f"Unknown niche: {requested}. "
                         f"Available: {[n['name'] for n in niches]}")
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
            max_results=8,
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

def produce_one(cfg: dict, secrets: Secrets, tracker: Tracker,
                niche_name: Optional[str]) -> Optional[int]:
    niche = pick_niche(cfg, niche_name)
    log.info("Niche: %s", niche["name"])

    candidates = discover_for(niche, cfg, secrets)
    log.info("Discovered %d candidates", len(candidates))

    for cand in candidates:
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
            # 1. Download
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

            # 2. Analyze
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
                model=cfg["ai"]["fallback_model"],  # text model is enough here
                style=cfg["script"]["style"],
            )
            tracker.update(vid, status="scripted",
                           script_text=json.dumps(script_obj))

            # 4. Voiceover
            vo = synthesize(script_obj["script"], vid,
                            api_key=secrets.elevenlabs_api_key,
                            voice_id=secrets.elevenlabs_voice_id,
                            model=cfg["voiceover"]["model"],
                            stability=cfg["voiceover"]["stability"],
                            similarity_boost=cfg["voiceover"]["similarity_boost"],
                            style=cfg["voiceover"]["style"],
                            speaker_boost=cfg["voiceover"]["speaker_boost"])
            if not vo:
                tracker.update(vid, status="failed", error="voiceover failed")
                continue
            vo = fit_audio_to_duration(vo, duration)
            tracker.update(vid, status="voiced", voiceover_path=str(vo))

            # 5. Edit
            edit_cfg = cfg["editing"]
            res = tuple(int(x) for x in edit_cfg["resolution"].split("x"))
            final = edit_video(
                source_video=local,
                voiceover_audio=vo,
                video_id=vid,
                niche=niche["name"],
                hook_text=script_obj.get("title", "")[:60],
                target_resolution=res,
                saturation=edit_cfg["filters"]["saturation"],
                sharpen=edit_cfg["filters"]["sharpen"],
                hdr_look=edit_cfg["filters"]["hdr_look"],
                music_volume_db=edit_cfg["background_music"]["volume_db"],
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
    today = datetime.utcnow()
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
    due = tracker.due_uploads(datetime.utcnow())
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
