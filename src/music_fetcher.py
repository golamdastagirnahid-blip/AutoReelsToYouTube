"""Jamendo music auto-fetcher.

Jamendo (https://www.jamendo.com) exposes 600,000+ Creative-Commons music
tracks via a free REST API. We pull CC-BY/CC-SA tagged tracks matching each
niche's `music_style`, cache them under `data/music/<niche>/`, and save an
attribution sidecar JSON next to every mp3 so the YouTube description can
credit the artist (legally required for CC-BY).

API docs: https://developer.jamendo.com/v3.0/tracks
Client ID: free signup at https://devportal.jamendo.com/

Environment variable: `JAMENDO_CLIENT_ID`.

Licenses requested (all monetization-safe with attribution):
    - ccby  : Creative Commons Attribution
    - ccsa  : Creative Commons Attribution-ShareAlike
(We SKIP ccnd/ccnc variants to avoid non-derivative or non-commercial clauses.)
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional, TypedDict

import requests

log = logging.getLogger(__name__)

JAMENDO_API = "https://api.jamendo.com/v3.0/tracks/"
JAMENDO_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 90

# Commercial-use-friendly CC licenses — require attribution only.
SAFE_LICENSES = "ccby,ccsa"


class Attribution(TypedDict):
    """Per-track attribution metadata written alongside each cached mp3."""
    artist: str
    title: str
    track_url: str
    license_name: str
    license_url: str
    source: str  # always "jamendo"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ensure_cache(
    *,
    niche: str,
    music_style: str,
    client_id: Optional[str],
    dest_root: Path,
    min_tracks: int = 3,
    fetch_count: int = 5,
) -> int:
    """Ensure `data/music/<niche>/` has at least `min_tracks` tracks.

    Fetches up to `fetch_count` new tracks from Jamendo if the folder is
    below `min_tracks`. Safe to call on every run — it's a cheap no-op
    once the cache is warm.

    Returns the number of NEW tracks downloaded this call.
    """
    if not client_id:
        return 0
    if not music_style or not music_style.strip():
        return 0

    dest_dir = dest_root / niche
    dest_dir.mkdir(parents=True, exist_ok=True)

    existing = _count_mp3s(dest_dir)
    if existing >= min_tracks:
        log.debug("Jamendo cache for %s already warm (%d tracks)",
                  niche, existing)
        return 0

    log.info("Jamendo: topping up music cache for '%s' "
             "(%d existing, fetching up to %d)…",
             niche, existing, fetch_count)

    try:
        candidates = _search(music_style, client_id, limit=fetch_count * 2)
    except Exception as e:
        log.warning("Jamendo API error: %s", e)
        return 0

    if not candidates:
        log.warning("Jamendo: no tracks returned for style=%r", music_style)
        return 0

    downloaded = 0
    needed = max(0, fetch_count - existing)
    for track in candidates:
        if downloaded >= needed:
            break
        mp3 = _download_track(track, dest_dir)
        if mp3:
            downloaded += 1

    log.info("Jamendo: cached %d new track(s) for %s", downloaded, niche)
    return downloaded


def attribution_for(mp3_path: Path) -> Optional[Attribution]:
    """Return attribution metadata for a cached mp3, or None if absent.

    Looks for `<mp3_path>.json` alongside the audio file.
    """
    if not mp3_path:
        return None
    sidecar = mp3_path.with_suffix(mp3_path.suffix + ".json")
    if not sidecar.exists():
        return None
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception as e:
        log.debug("Corrupt attribution sidecar %s: %s", sidecar, e)
        return None
    # Basic schema check
    if not isinstance(data, dict) or "artist" not in data:
        return None
    return data  # type: ignore[return-value]


def format_credit(attr: Attribution) -> str:
    """One-line human-readable music credit for video descriptions."""
    return (
        f"🎵 Music: \"{attr.get('title', 'Untitled')}\" by "
        f"{attr.get('artist', 'Unknown')} — "
        f"{attr.get('license_name', 'CC')} via Jamendo"
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _search(music_style: str, client_id: str, limit: int = 10) -> list[dict]:
    """Query Jamendo for tracks matching the style.

    `music_style` is something like "epic, cinematic, uplifting". We use
    each comma-separated token as a tag and let Jamendo do "OR matching".
    Results are sorted by all-time popularity (quality proxy).
    """
    tags = [t.strip().replace(" ", "") for t in music_style.split(",") if t.strip()]
    if not tags:
        return []
    # Jamendo wants '+'-separated tag list for OR match
    tag_query = "+".join(tags[:3])

    params = {
        "client_id": client_id,
        "format": "json",
        "limit": str(limit),
        "tags": tag_query,
        "include": "musicinfo+licenses",
        "audioformat": "mp32",          # 192kbps mp3
        "audiodlformat": "mp32",
        "order": "popularity_total",    # most-downloaded = likely best mastered
        "ccsa": "true",                 # allow CC-SA
        "ccnd": "false",                # disallow no-derivatives
        "ccnc": "false",                # disallow non-commercial
    }
    r = requests.get(JAMENDO_API, params=params, timeout=JAMENDO_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data.get("results") or []


def _download_track(track: dict, dest_dir: Path) -> Optional[Path]:
    """Download one Jamendo track + write attribution sidecar. Skip on failure."""
    audio_url = track.get("audiodownload") or track.get("audio")
    if not audio_url:
        return None

    track_id = track.get("id") or "unknown"
    artist = track.get("artist_name") or "Unknown"
    title = track.get("name") or "Untitled"

    safe = _sanitize(f"{artist}_{title}_{track_id}")
    mp3_path = dest_dir / f"{safe}.mp3"
    if mp3_path.exists():
        return mp3_path

    try:
        with requests.get(audio_url, stream=True, timeout=DOWNLOAD_TIMEOUT) as resp:
            resp.raise_for_status()
            with open(mp3_path, "wb") as f:
                for chunk in resp.iter_content(1 << 16):
                    if chunk:
                        f.write(chunk)
    except Exception as e:
        log.warning("Jamendo download failed (%s - %s): %s", artist, title, e)
        if mp3_path.exists():
            try:
                mp3_path.unlink()
            except Exception:
                pass
        return None

    # Write attribution sidecar (`<file>.mp3.json`)
    license_url = (
        track.get("license_ccurl")
        or (track.get("licenses", [{}])[0] if track.get("licenses") else {}).get("url", "")
        or "https://creativecommons.org/licenses/by/3.0/"
    )
    license_name = _license_name_from_url(license_url)
    attr: Attribution = {
        "artist": artist,
        "title": title,
        "track_url": f"https://www.jamendo.com/track/{track_id}",
        "license_name": license_name,
        "license_url": license_url,
        "source": "jamendo",
    }
    sidecar = mp3_path.with_suffix(mp3_path.suffix + ".json")
    sidecar.write_text(json.dumps(attr, indent=2, ensure_ascii=False),
                       encoding="utf-8")

    log.info("Jamendo: downloaded %s — %s (%s)", artist, title, license_name)
    return mp3_path


def _count_mp3s(folder: Path) -> int:
    if not folder.exists():
        return 0
    return len(list(folder.glob("*.mp3"))) + len(list(folder.glob("*.wav")))


_SAFE_CHARS = re.compile(r"[^a-zA-Z0-9._-]")


def _sanitize(s: str) -> str:
    out = _SAFE_CHARS.sub("_", s)
    out = re.sub(r"_+", "_", out).strip("_.")
    return out[:80] or "track"


def _license_name_from_url(url: str) -> str:
    u = (url or "").lower()
    if "by-sa" in u:
        return "CC BY-SA"
    if "by-nc" in u:
        return "CC BY-NC"
    if "by-nd" in u:
        return "CC BY-ND"
    if "by" in u:
        return "CC BY"
    return "Creative Commons"


# CLI self-test: python -m src.music_fetcher
if __name__ == "__main__":
    import os
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(message)s")
    cid = os.getenv("JAMENDO_CLIENT_ID")
    if not cid:
        print("Set JAMENDO_CLIENT_ID env var first.", file=sys.stderr)
        raise SystemExit(1)
    _PROJECT = Path(__file__).resolve().parents[1]
    style = sys.argv[1] if len(sys.argv) > 1 else "epic, cinematic, uplifting"
    niche = sys.argv[2] if len(sys.argv) > 2 else "motivation"
    n = ensure_cache(
        niche=niche,
        music_style=style,
        client_id=cid,
        dest_root=_PROJECT / "data" / "music",
        min_tracks=3,
        fetch_count=5,
    )
    print(f"Done. {n} new track(s) downloaded for niche={niche}.")
