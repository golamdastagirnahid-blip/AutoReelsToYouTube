"""Find the correct polling endpoint for an ai33.pro task_id.

Submits one Edge TTS job (cheapest), gets task_id, then probes many candidate
polling URLs and prints which one returns the audio result.

Usage:
    python -m src.utils.check_tts_poll
"""
from __future__ import annotations

import json
import time

import requests

from src.utils.secrets import Secrets

BASE_URL = "https://api.ai33.pro"


def main() -> int:
    s = Secrets.load()
    headers = {
        "xi-api-key": s.elevenlabs_api_key,
        "Content-Type": "application/json",
        "Accept": "application/json, audio/mpeg",
    }

    # Submit a tiny Edge job
    payload = {
        "text": "Hello, this is a test.",
        "voice": "en-US-BrianMultilingualNeural",
        "speed": 1,
        "with_transcript": False,
    }
    print("Submitting Edge TTS job...")
    r = requests.post(f"{BASE_URL}/v1e/task/text-to-speech",
                      headers=headers, json=payload, timeout=60)
    print(f"Submit status: {r.status_code}")
    print(f"Submit body  : {r.text[:300]}\n")
    if r.status_code != 200:
        return 1
    task_id = r.json().get("task_id")
    if not task_id:
        print("No task_id in response")
        return 1
    print(f"task_id = {task_id}\n")

    # Candidate polling URLs (GET first, POST second if GET fails)
    candidates = [
        f"/v1e/task/{task_id}",
        f"/v1e/task/result/{task_id}",
        f"/v1e/task/status/{task_id}",
        f"/v1/task/{task_id}",
        f"/v1/task/result/{task_id}",
        f"/v1/task/status/{task_id}",
        f"/v1/tasks/{task_id}",
        f"/v1e/tasks/{task_id}",
        f"/v1e/result/{task_id}",
        f"/v1/result/{task_id}",
    ]

    print("Probing GET endpoints (waiting 5s for task to process first)...")
    time.sleep(5)
    found = []
    for path in candidates:
        url = f"{BASE_URL}{path}"
        try:
            resp = requests.get(url, headers=headers, timeout=15)
        except Exception as e:
            print(f"GET {path:<40} -> ERROR {e}")
            continue
        ct = resp.headers.get("Content-Type", "")
        size = len(resp.content)
        snip = resp.text[:200] if "json" in ct or "text" in ct else f"<{size} bytes>"
        marker = "  <-- WORKING" if resp.status_code == 200 and resp.text.strip() else ""
        print(f"GET {path:<40} -> {resp.status_code}  {ct[:30]:<30}  {snip[:120]}{marker}")
        if resp.status_code == 200 and ("audio" in ct or "url" in resp.text or "file" in resp.text):
            found.append((path, resp))

    if found:
        print("\n" + "=" * 60)
        print("WORKING POLLING ENDPOINTS FOUND:")
        for path, resp in found:
            print(f"\n  {path}")
            try:
                print(f"  JSON keys: {list(resp.json().keys())}")
                print(f"  Full body: {json.dumps(resp.json(), indent=2)[:600]}")
            except Exception:
                print(f"  (binary, {len(resp.content)} bytes)")
    else:
        print("\nNo working polling endpoints found from GET. The API may use webhooks only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
