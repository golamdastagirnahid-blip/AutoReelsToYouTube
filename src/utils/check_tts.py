"""Diagnostic for the ai33.pro TTS proxy.

Calls each of the three TTS endpoints with a short test phrase and prints:
  - HTTP status code
  - Response headers (Content-Type)
  - First 500 chars of the response body (or audio file size)
  - What we think went wrong

Usage:
    python -m src.utils.check_tts
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

from src.utils.secrets import Secrets

BASE_URL = "https://api.ai33.pro"
TEST_TEXT = "Hello, this is a test of the text to speech system."


def _print(name: str, status: int, headers: dict, body_preview: str,
           is_audio: bool, audio_size: int = 0) -> None:
    print(f"\n{'=' * 60}")
    print(f"BACKEND: {name}")
    print(f"{'=' * 60}")
    print(f"Status     : {status}")
    print(f"Content-Type: {headers.get('Content-Type', '?')}")
    if is_audio:
        print(f"Audio bytes: {audio_size:,} (looks like a real audio file)")
    print(f"Body       : {body_preview[:500]}")
    if status == 200 and is_audio:
        print("Verdict    : OK")
    elif status == 401:
        print("Verdict    : UNAUTHORIZED — API key invalid or no permission for this backend")
    elif status == 402 or status == 429:
        print("Verdict    : OUT OF CREDIT or rate-limited")
    elif status == 404:
        print("Verdict    : Endpoint or voice ID not found")
    elif status >= 500:
        print("Verdict    : Server error on the proxy side")
    elif status == 200:
        print("Verdict    : Got 200 but response is JSON (likely async task) — check body")
    else:
        print(f"Verdict    : Unexpected status {status}")


def _is_audio(content: bytes) -> bool:
    if len(content) < 100:
        return False
    head = content[:4]
    if head.startswith(b"ID3") or head.startswith(b"RIFF"):
        return True
    if content[0] == 0xFF and content[1] in (0xFB, 0xF3, 0xF2):
        return True
    return False


def hit(name: str, path: str, payload: dict, api_key: str) -> None:
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg, application/json",
    }
    try:
        r = requests.post(f"{BASE_URL}{path}", headers=headers, json=payload, timeout=60)
    except Exception as e:
        print(f"\n=== {name}: REQUEST FAILED — {e} ===")
        return

    is_aud = _is_audio(r.content)
    if is_aud:
        body = "(binary audio data)"
    else:
        try:
            body = json.dumps(r.json(), indent=2)
        except Exception:
            body = r.text or "(empty body)"

    _print(name, r.status_code, dict(r.headers), body, is_aud, len(r.content))


def main() -> int:
    s = Secrets.load()
    print(f"Using API key: {s.elevenlabs_api_key[:6]}...{s.elevenlabs_api_key[-4:]}")
    print(f"Test text   : {TEST_TEXT!r}")

    # 1. ElevenLabs-style
    hit(
        "ElevenLabs",
        f"/v1/text-to-speech/{s.elevenlabs_voice_id}?output_format=mp3_44100_128",
        {"text": TEST_TEXT, "model_id": "eleven_multilingual_v2"},
        s.elevenlabs_api_key,
    )

    # 2. MiniMax
    hit(
        "MiniMax",
        "/v1m/task/text-to-speech",
        {
            "text": TEST_TEXT,
            "model": "speech-2.6-hd",
            "voice_setting": {"voice_id": "209533299589184", "vol": 1, "pitch": 0, "speed": 1},
            "language_boost": "Auto",
        },
        s.elevenlabs_api_key,
    )

    # 3. Edge Neural
    hit(
        "Edge (Brian)",
        "/v1e/task/text-to-speech",
        {
            "text": TEST_TEXT,
            "voice": "en-US-BrianMultilingualNeural",
            "speed": 1,
            "with_transcript": False,
            "with_loudnorm": False,
        },
        s.elevenlabs_api_key,
    )

    # Try a /credits or /balance endpoint (common naming)
    print("\n" + "=" * 60)
    print("Probing for account/credit endpoint...")
    print("=" * 60)
    for path in ("/v1/user", "/v1/account", "/v1/credits", "/v1/balance",
                 "/v1/user/subscription"):
        try:
            r = requests.get(f"{BASE_URL}{path}",
                             headers={"xi-api-key": s.elevenlabs_api_key}, timeout=15)
            print(f"GET {path:<28} -> {r.status_code}  {r.text[:200]}")
        except Exception as e:
            print(f"GET {path:<28} -> ERROR  {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
