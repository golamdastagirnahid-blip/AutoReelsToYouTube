"""One-time helper to obtain a YouTube refresh token.

Run this LOCALLY (not in GitHub Actions) once after you have:
  - YOUTUBE_CLIENT_ID
  - YOUTUBE_CLIENT_SECRET
in your .env file.

Usage:
    python -m src.utils.get_youtube_token

It opens your browser, you sign in to your YouTube channel's Google account,
grant permissions, and the script prints a refresh token.

Copy that token into GitHub Secrets as YOUTUBE_REFRESH_TOKEN.
You only need to do this once.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from src.utils.secrets import Secrets

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]


def main() -> int:
    s = Secrets.load()

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
        print("ERROR: No refresh token returned. Try again with a fresh consent.",
              file=sys.stderr)
        return 1

    print("\n" + "=" * 60)
    print("SUCCESS — Copy this value into GitHub Secrets:")
    print("=" * 60)
    print(f"\nYOUTUBE_REFRESH_TOKEN={creds.refresh_token}\n")
    print("=" * 60)

    # Also save locally for convenience (gitignored)
    out = Path(__file__).resolve().parents[2] / "youtube_token.json"
    out.write_text(json.dumps({"refresh_token": creds.refresh_token}, indent=2))
    print(f"Saved a local copy at: {out} (gitignored)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
