"""Pre-flight health check — verify every credential & external dependency.

Run before a long pipeline run to catch issues fast:

    python -m src.main check

Each check returns (ok: bool, msg: str). The runner prints a coloured
pass/fail report and exits non-zero if any REQUIRED check fails.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from src.utils.secrets import Secrets, _mask

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ============================================================
# Individual checks — each returns (ok, detail_msg)
# ============================================================

def _check_secret_loaded(name: str, val: Optional[str]) -> tuple[bool, str]:
    if not val:
        return False, "missing — set in .env or GitHub Secrets"
    return True, _mask(val)


def _check_nvidia(api_key: str) -> tuple[bool, str]:
    """Hit NVIDIA's chat endpoint with a 1-token request."""
    try:
        r = requests.post(
            "https://integrate.api.nvidia.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"model": "meta/llama-3.1-70b-instruct",
                  "messages": [{"role": "user", "content": "hi"}],
                  "max_tokens": 1, "temperature": 0.0},
            timeout=15,
        )
        if r.status_code == 200:
            return True, "OK (chat completion responded)"
        return False, f"HTTP {r.status_code}: {r.text[:120]}"
    except requests.RequestException as e:
        return False, f"network error: {e}"


def _check_elevenlabs(api_key: str) -> tuple[bool, str]:
    """Hit ElevenLabs /v1/user — works even on free plans."""
    try:
        r = requests.get(
            "https://api.elevenlabs.io/v1/user",
            headers={"xi-api-key": api_key},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            sub = data.get("subscription", {})
            chars_left = (sub.get("character_limit", 0)
                          - sub.get("character_count", 0))
            return True, f"OK (tier={sub.get('tier', '?')}, "\
                         f"~{chars_left} chars remaining)"
        if r.status_code == 401:
            return False, "401 unauthorized — bad/expired API key"
        return False, f"HTTP {r.status_code}"
    except requests.RequestException as e:
        return False, f"network error: {e}"


def _check_pexels(api_key: Optional[str]) -> tuple[bool, str]:
    if not api_key:
        return False, "key missing (optional)"
    try:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": api_key},
            params={"query": "ocean", "per_page": 1},
            timeout=10,
        )
        if r.status_code == 200:
            remaining = r.headers.get("X-Ratelimit-Remaining", "?")
            return True, f"OK (rate-limit remaining: {remaining})"
        if r.status_code == 401:
            return False, "401 unauthorized — bad API key"
        return False, f"HTTP {r.status_code}"
    except requests.RequestException as e:
        return False, f"network error: {e}"


def _check_pixabay(api_key: Optional[str]) -> tuple[bool, str]:
    if not api_key:
        return False, "key missing (optional)"
    try:
        r = requests.get(
            "https://pixabay.com/api/videos/",
            params={"key": api_key, "q": "ocean", "per_page": 3},
            timeout=10,
        )
        if r.status_code == 200:
            total = r.json().get("total", 0)
            return True, f"OK ({total} matching videos for test query)"
        return False, f"HTTP {r.status_code}: {r.text[:120]}"
    except requests.RequestException as e:
        return False, f"network error: {e}"


def _check_unsplash(api_key: Optional[str]) -> tuple[bool, str]:
    if not api_key:
        return False, "key missing (optional)"
    try:
        r = requests.get(
            "https://api.unsplash.com/search/photos",
            headers={"Authorization": f"Client-ID {api_key}"},
            params={"query": "ocean", "per_page": 1},
            timeout=10,
        )
        if r.status_code == 200:
            remaining = r.headers.get("X-Ratelimit-Remaining", "?")
            return True, f"OK (rate-limit remaining: {remaining})"
        if r.status_code == 401:
            return False, "401 unauthorized — bad access key"
        return False, f"HTTP {r.status_code}"
    except requests.RequestException as e:
        return False, f"network error: {e}"


def _check_jamendo(client_id: Optional[str]) -> tuple[bool, str]:
    """Verify Jamendo API key via a 1-track query. Optional."""
    if not client_id:
        return False, "key missing (optional)"
    try:
        r = requests.get(
            "https://api.jamendo.com/v3.0/tracks/",
            params={"client_id": client_id, "format": "json",
                    "limit": "1", "tags": "cinematic"},
            timeout=10,
        )
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:120]}"
        data = r.json()
        status = (data.get("headers") or {}).get("status", "")
        if status == "success":
            total = (data.get("headers") or {}).get("results_count", "?")
            return True, f"OK (catalog reachable, {total} cinematic matches)"
        err = (data.get("headers") or {}).get("error_message", "unknown")
        return False, f"API error: {err}"
    except requests.RequestException as e:
        return False, f"network error: {e}"


def _check_youtube_token(client_id: str, client_secret: str,
                        refresh_token: Optional[str]) -> tuple[bool, str]:
    """The most important check: actually exchange the refresh token for
    an access token AND verify the channel is reachable + has upload scope.
    """
    if not refresh_token:
        return False, "YOUTUBE_REFRESH_TOKEN not set — uploads will be skipped"
    try:
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=[
                "https://www.googleapis.com/auth/youtube.upload",
                "https://www.googleapis.com/auth/youtube",
            ],
        )
        creds.refresh(Request())
        # Got an access token — now confirm the channel and scope are valid
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/channels",
            headers={"Authorization": f"Bearer {creds.token}"},
            params={"part": "snippet,contentDetails,status", "mine": "true"},
            timeout=10,
        )
        if r.status_code != 200:
            return False, f"channels.list HTTP {r.status_code}: " \
                          f"{r.text[:200]}"
        items = r.json().get("items", [])
        if not items:
            return False, "no channels returned — token may lack youtube scope"
        ch = items[0]
        title = ch["snippet"]["title"]
        ch_id = ch["id"]
        # Check upload-permitting status
        privacy = ch.get("status", {}).get("privacyStatus", "?")
        return True, f"OK — channel='{title}' (id={ch_id}, " \
                     f"privacy={privacy})"
    except Exception as e:
        msg = str(e)
        if "invalid_grant" in msg:
            return False, ("invalid_grant — refresh token expired or "
                          "revoked. Regenerate via OAuth flow.")
        if "invalid_client" in msg:
            return False, ("invalid_client — CLIENT_ID/SECRET don't match "
                          "the project that issued the refresh token.")
        return False, f"refresh failed: {msg[:200]}"


def _check_ffmpeg() -> tuple[bool, str]:
    if not shutil.which("ffmpeg"):
        return False, "ffmpeg not on PATH"
    try:
        out = subprocess.run(["ffmpeg", "-version"],
                             capture_output=True, text=True, timeout=5)
        first = out.stdout.splitlines()[0] if out.stdout else "?"
        return True, first[:80]
    except Exception as e:
        return False, str(e)


def _check_config() -> tuple[bool, str]:
    """Quick config sanity: dry_run setting and upload window."""
    try:
        import yaml
        with open(_PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        dry = cfg.get("safety", {}).get("dry_run", True)
        upload = cfg.get("upload", {})
        win_s = upload.get("window_start_hour", "?")
        win_e = upload.get("window_end_hour", "?")
        per_day = upload.get("videos_per_day", "?")
        prefix = "LIVE" if not dry else "DRY-RUN"
        return True, (f"{prefix} mode | window {win_s}:00-{win_e}:00 UTC "
                     f"| {per_day}/day")
    except Exception as e:
        return False, f"config load failed: {e}"


def _check_data_dirs() -> tuple[bool, str]:
    """Report on processed videos and music library."""
    proc_dir = _PROJECT_ROOT / "data" / "processed"
    music_dir = _PROJECT_ROOT / "data" / "music"
    n_proc = len(list(proc_dir.glob("*_final.mp4"))) if proc_dir.exists() else 0
    n_music = (len(list(music_dir.glob("**/*.mp3")))
               + len(list(music_dir.glob("**/*.wav")))) \
              if music_dir.exists() else 0
    return True, (f"{n_proc} produced video(s) cached, "
                 f"{n_music} music track(s) available")


# ============================================================
# Runner
# ============================================================

# Each entry: (name, check_fn_args, required_for_pipeline)
def _run_all(secrets: Secrets) -> list[tuple[str, bool, str, bool]]:
    """Returns a list of (name, ok, detail, required)."""
    checks: list[tuple[str, Callable[[], tuple[bool, str]], bool]] = [
        ("ffmpeg",
         _check_ffmpeg, True),
        ("config.yaml",
         _check_config, True),
        ("NVIDIA API",
         lambda: _check_nvidia(secrets.nvidia_api_key), True),
        ("ElevenLabs API",
         lambda: _check_elevenlabs(secrets.elevenlabs_api_key), False),
        ("Pexels API",
         lambda: _check_pexels(secrets.pexels_api_key), False),
        ("Pixabay API",
         lambda: _check_pixabay(secrets.pixabay_api_key), False),
        ("Unsplash API",
         lambda: _check_unsplash(secrets.unsplash_access_key), False),
        ("Jamendo API (music)",
         lambda: _check_jamendo(secrets.jamendo_client_id), False),
        ("YouTube OAuth (refresh+channel)",
         lambda: _check_youtube_token(
             secrets.youtube_client_id,
             secrets.youtube_client_secret,
             secrets.youtube_refresh_token), True),
        ("data dirs",
         _check_data_dirs, False),
    ]
    results: list[tuple[str, bool, str, bool]] = []
    for name, fn, required in checks:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"check raised: {e}"
        results.append((name, ok, detail, required))
    return results


def run(secrets: Optional[Secrets] = None,
        *, raise_on_required_failure: bool = False) -> bool:
    """Run all checks and print a coloured pass/fail report.

    Returns True if all REQUIRED checks pass.
    Raises RuntimeError if `raise_on_required_failure` is set and any required
    check fails (used at pipeline start to abort early).
    """
    if secrets is None:
        secrets = Secrets.load()

    results = _run_all(secrets)

    # Print report
    print("\n" + "=" * 66)
    print("  AutoReels Health Check")
    print("=" * 66)
    for name, ok, detail, required in results:
        mark = "[ OK ]" if ok else ("[FAIL]" if required else "[SKIP]")
        tag = " (required)" if required and not ok else ""
        print(f"  {mark}  {name:<32} {detail}{tag}")
    print("=" * 66)

    required_failures = [r for r in results if r[3] and not r[1]]
    if required_failures:
        names = ", ".join(r[0] for r in required_failures)
        msg = f"Required check(s) failed: {names}"
        print(f"  ✗ {msg}\n")
        if raise_on_required_failure:
            raise RuntimeError(msg)
        return False

    optional_failures = [r for r in results if not r[3] and not r[1]]
    if optional_failures:
        names = ", ".join(r[0] for r in optional_failures)
        print(f"  ! Optional issues: {names}\n")
    else:
        print("  ✓ All systems go!\n")
    return True


if __name__ == "__main__":
    import sys
    ok = run()
    sys.exit(0 if ok else 1)
