"""Stage 4 — Generate a voiceover script that matches the video.

Produces:
  - script_text: voiceover narration (timed to match duration)
  - title:       SEO-optimized YouTube title
  - description: short body for the YouTube description
  - tags:        list of YouTube tags
  - hashtags:    list of hashtags for the description
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from openai import OpenAI

log = logging.getLogger(__name__)

_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
_DEFAULT_MODEL = "meta/llama-3.1-70b-instruct"

_SYSTEM = """You write scripts for faceless YouTube Shorts.

Your output MUST be valid JSON with this exact shape:
{
  "title": "60-char SEO-friendly title with curiosity hook",
  "script": "voiceover text, no stage directions, no emojis, plain spoken English",
  "description": "2-3 sentence YouTube description body",
  "tags": ["tag1", "tag2", ...],   // 8-15 tags, lowercase, no #
  "hashtags": ["#shorts", "#niche", ...]  // 4-6 hashtags
}

Hard rules:
- Script must hook in the first 3 seconds (a question, a shocking fact, or a contrarian claim)
- Tone is informative, professional, engaging — never salesy, never clickbait
- Word count = roughly duration_sec * 2.5 (~150 wpm)
- No promotional content, no calls to subscribe inside the script
- No mentions of the original creator inside the script (credit goes in description only)
- Do NOT make up specific statistics that weren't in the analysis
"""


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"No JSON in response: {text[:200]}")
    return json.loads(m.group(0))


def write_script(
    analysis: dict,
    duration_sec: float,
    niche: str,
    tone: str,
    nvidia_api_key: str,
    *,
    model: str = _DEFAULT_MODEL,
    style: str = "informative, professional, engaging",
) -> dict:
    target_words = max(15, int(duration_sec * 2.5))
    user_prompt = (
        f"Niche: {niche}\n"
        f"Desired tone: {tone}\n"
        f"Style guide: {style}\n"
        f"Duration: {duration_sec:.1f}s (target ~{target_words} words)\n\n"
        f"Video analysis:\n{json.dumps(analysis, ensure_ascii=False)[:3000]}\n\n"
        f"Write the JSON output now."
    )
    client = OpenAI(api_key=nvidia_api_key, base_url=_NVIDIA_BASE_URL)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
        max_tokens=900,
    )
    out = _parse_json(resp.choices[0].message.content)

    # Validate shape with sane defaults
    out.setdefault("title", f"Amazing {niche} short")
    out.setdefault("script", "")
    out.setdefault("description", "")
    out.setdefault("tags", [])
    out.setdefault("hashtags", ["#shorts", f"#{niche.replace('_', '')}"])

    # Trim title to YouTube limit
    out["title"] = str(out["title"])[:95]
    return out
