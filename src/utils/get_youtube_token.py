"""One-time helper to obtain a YouTube refresh token.

Run this LOCALLY (not in GitHub Actions) once after you have:
  - YOUTUBE_CLIENT_ID
  - YOUTUBE_CLIENT_SECRET
in your .env file.

Usage:
    python -m src.utils.get_youtube_token

It opens your browser, you sign in to your YouTube channel's Google account,
grant permissions, and the script prints a refresh token.

After running:
  1. Copy the printed token into your local .env as YOUTUBE_REFRESH_TOKEN=...
  2. Update the GitHub Secret YOUTUBE_REFRESH_TOKEN with the same value
  3. Run `python -m src.main check` to verify the channel is reachable
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests
from google_auth_oauthlib.flow import InstalledAppFlow

from src.utils.secrets import Secrets

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]


def _verify_channel(access_token: str) -> tuple[bool, str]:
    """Call channels.list?mine=true to confirm a channel exists for this token."""
    try:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/channels",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"part": "snippet", "mine": "true"},
            timeout=10,
        )
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        items = r.json().get("items", [])
        if not items:
            return False, "no channels — wrong account?"
        ch = items[0]["snippet"]
        return True, f"'{ch['title']}' (id={items[0]['id']})"
    except requests.RequestException as e:
        return False, f"network error: {e}"


def main() -> int:
    s = Secrets.load()

    print("\n" + "=" * 66)
    print("  YouTube refresh-token wizard")
    print("=" * 66)
    print("  1. Your browser will open in a moment.")
    print("  2. Sign in with the Google account THAT OWNS YOUR YOUTUBE CHANNEL.")
    print("  3. Grant the YouTube upload + manage permissions.")
    print("  4. Come back here — the new refresh token will be printed.")
    print("=" * 66)
    input("  Press ENTER to open the browser...")

    client_config = {
        "installed": {
            "client_id": s.youtube_client_id,
            "client_secret": s.youtube_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    if not creds.refresh_token:
        print("\nERROR: No refresh token returned. This usually means you've already "
              "authorized this app once. Go to "
              "https://myaccount.google.com/permissions, find this app, click "
              "'Remove access', then re-run this script.", file=sys.stderr)
        return 1

    # Verify the channel BEFORE printing the token, so we don't overwrite the
    # GitHub Secret with another wrong-account token.
    print("\nVerifying the channel for this account...")
    ok, detail = _verify_channel(creds.token)
    if not ok:
        print(f"\nFAIL — channels.list returned: {detail}", file=sys.stderr)
        print("\nThe account you signed in with does NOT have a YouTube channel.",
              file=sys.stderr)
        print("Either create one at youtube.com, or re-run this script and sign "
              "in with a different Google account that owns a channel.",
              file=sys.stderr)
        return 1

    print(f"OK — verified channel: {detail}\n")

    print("=" * 66)
    print("  SUCCESS — paste this into your .env AND your GitHub Secret")
    print("=" * 66)
    print(f"\nYOUTUBE_REFRESH_TOKEN={creds.refresh_token}\n")
    print("=" * 66)
    print("  GitHub Secrets URL:")
    print("  https://github.com/golamdastagirnahid-blip/AutoReelsToYouTube"
          "/settings/secrets/actions")
    print("=" * 66)

    # Save a local convenience copy (gitignored)
    out = Path(__file__).resolve().parents[2] / "youtube_token.json"
    out.write_text(json.dumps({"refresh_token": creds.refresh_token,
                               "channel": detail}, indent=2))
    print(f"\nSaved a local copy at: {out} (gitignored)\n")
    print("Next step: run `python -m src.main check` to confirm everything works.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
