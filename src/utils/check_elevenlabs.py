"""Diagnostic: verify ElevenLabs API key + list accessible voices.

Usage:
    python -m src.utils.check_elevenlabs
"""
from __future__ import annotations

import sys

import requests

from src.utils.secrets import Secrets


def main() -> int:
    s = Secrets.load()

    print("Testing ElevenLabs API key...")
    r = requests.get(
        "https://api.elevenlabs.io/v1/voices",
        headers={"xi-api-key": s.elevenlabs_api_key},
        timeout=30,
    )

    if r.status_code == 401:
        print("ERROR: 401 Unauthorized — your API key is invalid or revoked.")
        print("Fix: get a new key from your ElevenLabs provider and update .env")
        return 1
    if r.status_code != 200:
        print(f"ERROR: unexpected status {r.status_code}: {r.text[:300]}")
        return 1

    voices = r.json().get("voices", [])
    print(f"\nOK — key works. {len(voices)} voices accessible:\n")
    print(f"  {'VOICE ID':<25}  {'NAME':<25}  CATEGORY")
    print(f"  {'-'*25}  {'-'*25}  {'-'*15}")
    for v in voices:
        vid = v.get("voice_id", "")
        name = v.get("name", "")
        cat = v.get("category", "")
        marker = "  <-- CURRENT" if vid == s.elevenlabs_voice_id else ""
        print(f"  {vid:<25}  {name:<25}  {cat}{marker}")

    # Show quota
    print("\nFetching quota...")
    u = requests.get(
        "https://api.elevenlabs.io/v1/user/subscription",
        headers={"xi-api-key": s.elevenlabs_api_key},
        timeout=30,
    )
    if u.status_code == 200:
        sub = u.json()
        used = sub.get("character_count", 0)
        lim = sub.get("character_limit", 0)
        tier = sub.get("tier", "?")
        print(f"  Tier: {tier}")
        print(f"  Used: {used:,} / {lim:,} characters")

    print(f"\nYour current ELEVENLABS_VOICE_ID = {s.elevenlabs_voice_id}")
    if not any(v.get("voice_id") == s.elevenlabs_voice_id for v in voices):
        print("  !!! That voice ID is NOT in the accessible list above.")
        print("  Pick one from the list and update ELEVENLABS_VOICE_ID in .env")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
