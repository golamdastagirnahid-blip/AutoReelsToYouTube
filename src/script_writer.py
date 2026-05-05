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

_SYSTEM = """You are a world-class YouTube Shorts scriptwriter. Your scripts have
generated hundreds of millions of views and earn YouTube Partner Program revenue.
You write for FACELESS channels — voiceover only, no host on camera.

OUTPUT — return ONLY valid JSON (no markdown, no preamble):
{
  "hook_overlay": "2-4 WORDS, max 14 chars, ALL CAPS, shown as on-screen text in first 3s",
  "title": "YouTube title (50-95 chars), curiosity-driven, includes a power word",
  "script": "voiceover text — plain spoken English, no stage directions, no emojis",
  "description": "2-4 sentence body. First sentence MUST repeat the hook to win the algorithm preview.",
  "tags": ["8-15 SEO tags, lowercase, no # symbol"],
  "hashtags": ["#shorts", "#nichetag", "..."]   // exactly 5 hashtags including #shorts
}

============================================================
THE THREE-ACT VIRAL FORMULA — follow this structure ALWAYS
============================================================

ACT 1 — HOOK (first 3 seconds, ~7-10 words)
   Pick ONE of these proven patterns:
   • Curiosity gap:    "Here's why [common belief] is completely wrong..."
   • Shock fact:       "94% of people don't know that..."  (only if backed by analysis)
   • Pattern break:    "Stop doing [common habit]. Do this instead."
   • Direct question:  "What if I told you [outcome] takes only [time]?"
   • Contrarian:       "Everyone says [X]. They're wrong."
   • Specific number:  "These 3 [things] will change your [outcome]."
   • POV/scenario:     "Imagine you [situation]. Here's what happens..."
   The hook MUST imply a payoff later — never give the answer in the hook.

ACT 2 — BODY (60-75% of duration, the meat)
   GOLDEN RULE: SMALL BUT EFFECTIVE — say ONE core thing, not three.
   A 28-second Short cannot teach three lessons. It can land ONE killer
   insight that the viewer remembers tomorrow. Pick the single most
   surprising / counter-intuitive / specific takeaway from the analysis
   and build the entire body around it.

   STRUCTURE:
   • Sentence 1 of body = the ONE core insight, said directly. No buildup.
   • Sentence 2-3 = ONE concrete detail that proves it (number, name,
     example, mechanism). Specific beats abstract every time.
   • Sentence 4 (optional) = a sharp contrast or twist that re-frames it.

   STYLE RULES:
   • Every sentence must EARN its place. If you can delete it without
     losing meaning, delete it. Filler kills retention.
   • Front-load the surprise. The most interesting fact comes FIRST,
     not after "let me explain" or "here's the thing".
   • Use SECOND PERSON ("you", "your") in most sentences.
   • Active verbs only — never passive voice.
   • Specific over generic: "300 calories" beats "some calories";
     "a blue whale's heart" beats "a large animal's heart".
   • Sentences max 10 words. Two short > one long.
   • Pattern interrupt every 5-7 words (new clause, new angle, new specific).

   BANNED FILLER PHRASES (drop these on sight):
   "Let me tell you", "the truth is", "the fact is", "believe it or not",
   "you won't believe", "basically", "literally", "actually", "honestly",
   "you know", "so…", "well…", "now…", "in this video", "today we'll…",
   "stay tuned", "keep watching", "here's the thing", "at the end of the day".

ACT 3 — LOOPBACK CTA (last 2-3 seconds, ~5-8 words)
   Critical for retention + replays (algorithm boost):
   • End with a sentence that re-frames or escalates the hook
   • OR a question that makes them want to watch again
   • OR a "save this" / "try this tomorrow" instruction
   • NEVER say "subscribe", "like", or mention the channel

============================================================
HARD CONSTRAINTS
============================================================

1.  WORD COUNT: aim for `target_words` ± 10%. NEVER under 80% of target.
2.  HOOK_OVERLAY: max 4 words, max 14 characters total, no punctuation.
    GOOD: "STOP DOING THIS", "94% FAIL THIS", "1 HABIT"
    BAD:  "The Three Things You Need To Know" (too long, will be cut off)
3.  ONE CORE INSIGHT per script. Not two, not three. ONE. The body proves
    that single insight with concrete detail. Multiple loose tips dilute
    retention; a single sharp idea wins.
4.  NO em-dashes, no semicolons, no parentheses in the script (TTS can't read them well).
5.  NO "subscribe", "like", "follow", channel name, or self-promo INSIDE the script.
6.  NO original-creator credits inside the script (those go in description only).
7.  NO made-up statistics — if the video analysis doesn't support a number, use ranges
    ("most people", "many studies") instead.
8.  NO filler / throat-clearing words at sentence starts: "so", "well", "now",
    "basically", "actually", "literally", "honestly". Open every sentence with
    a noun, verb, or number.
9.  TITLE: must contain at least one POWER WORD: secret, truth, hidden, proven, science,
    real, why, how, never, always, instantly, 1-minute, ultimate, mistake.
10. Tone matches the niche (provided below) but always cinematic + confident.
11. NO emojis ANYWHERE. NO hashtags inside the script or title.
12. Output MUST be parseable JSON — no trailing commas, no comments.

============================================================
SELF-CHECK before responding
============================================================
☐ Does the hook (first sentence of script) create a curiosity gap?
☐ Is hook_overlay ≤ 14 chars and ≤ 4 words?
☐ Is there exactly ONE core insight in the body, not multiple competing ones?
☐ Does the body's first sentence state the surprise directly (no buildup)?
☐ Can ANY sentence be deleted without losing meaning? If yes, delete it.
☐ Are all sentences ≤ 10 words and active-voice?
☐ Are there ZERO filler phrases ("basically", "actually", "let me tell you", etc.)?
☐ Does the last sentence loop back / give a save-worthy takeaway?
☐ Word count within ±10% of target?
☐ Title has a power word and is under 95 chars?
☐ No emojis, no #, no "subscribe"?
"""


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"No JSON in response: {text[:200]}")
    return json.loads(m.group(0))


_TTS_UNFRIENDLY = re.compile(r"[—–;()\[\]{}*_`|]")
_MULTI_PUNCT = re.compile(r"([.!?])\1{2,}")
_MULTI_SPACE = re.compile(r"\s+")


def _clean_for_tts(text: str) -> str:
    """Strip TTS-unfriendly punctuation and collapse whitespace."""
    if not text:
        return ""
    text = _TTS_UNFRIENDLY.sub(" ", text)
    text = _MULTI_PUNCT.sub(r"\1", text)
    text = _MULTI_SPACE.sub(" ", text).strip()
    return text


def _shorten_hook(hook: str, max_words: int = 4, max_chars: int = 14) -> str:
    """Force the on-screen hook overlay into safe-zone text:
    - ALL CAPS
    - <= max_words words
    - <= max_chars total
    Falls back to the first N words of the script if needed.
    """
    if not hook:
        return ""
    h = re.sub(r"[^\w\s]", "", hook).strip().upper()
    words = h.split()[:max_words]
    while words and sum(len(w) for w in words) + len(words) - 1 > max_chars:
        words.pop()
    return " ".join(words) if words else ""


def write_script(
    analysis: dict,
    duration_sec: float,
    niche: str,
    tone: str,
    nvidia_api_key: str,
    *,
    model: str = _DEFAULT_MODEL,
    style: str = "informative, professional, engaging",
    min_duration_sec: float = 22.0,
    trending_titles: Optional[list[str]] = None,
) -> dict:
    """Generate a viral Shorts script with a strict 3-act structure.

    Args:
        min_duration_sec: enforce a minimum target duration (default 22s).
            YouTube Shorts under 20s have notably worse retention/CPM.
            We bump short videos up to give the script room for hook + body + payoff.
        trending_titles: optional list of Reddit top-of-day post titles for
            the niche. When provided they're injected into the prompt as
            "what people are discussing right now" context so the script
            stays tied to live conversation. Titles are used as INSPIRATION
            only — the script must NOT copy them verbatim.
    """
    # Force a minimum target so we don't end up with a 7-second video
    effective_duration = max(min_duration_sec, duration_sec)
    target_words = max(60, int(effective_duration * 2.5))

    # Optional "what people are talking about right now" block.
    # Capped at 8 titles and 140 chars each to keep the prompt compact.
    trending_block = ""
    if trending_titles:
        clipped = [
            (t[:140].rstrip() + ("…" if len(t) > 140 else ""))
            for t in trending_titles[:8] if t
        ]
        if clipped:
            trending_block = (
                "Trending discussion titles in this niche RIGHT NOW "
                "(from Reddit top-of-day — use as INSPIRATION for a timely "
                "hook or angle, do NOT copy verbatim, do NOT mention Reddit):\n"
                + "\n".join(f"  - {t}" for t in clipped)
                + "\n\n"
            )

    user_prompt = (
        f"Niche: {niche}\n"
        f"Desired tone: {tone}\n"
        f"Style guide: {style}\n"
        f"Target duration: {effective_duration:.1f}s "
        f"(WRITE EXACTLY {target_words} words ± 10%)\n\n"
        f"Video analysis (use as INSPIRATION for visuals/topic, do not copy):\n"
        f"{json.dumps(analysis, ensure_ascii=False)[:3000]}\n\n"
        f"{trending_block}"
        f"Now write the JSON output. Self-check before submitting."
    )
    client = OpenAI(api_key=nvidia_api_key, base_url=_NVIDIA_BASE_URL)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.85,           # higher = more creative/varied hooks
        top_p=0.95,
        max_tokens=1100,
    )
    out = _parse_json(resp.choices[0].message.content)

    # ---- Validate / sanitize each field ----
    out["script"] = _clean_for_tts(str(out.get("script", "")))
    out["title"] = _clean_for_tts(str(out.get("title", f"Amazing {niche} short")))[:95]
    out["description"] = _clean_for_tts(str(out.get("description", "")))

    # Hook overlay: clamp to safe-zone size; if missing, derive from first words
    hook = str(out.get("hook_overlay", "")).strip()
    if not hook and out["script"]:
        hook = " ".join(out["script"].split()[:4])
    out["hook_overlay"] = _shorten_hook(hook)

    # Tags & hashtags
    raw_tags = out.get("tags") or []
    out["tags"] = [str(t).strip().lstrip("#").lower()
                   for t in raw_tags if str(t).strip()][:15]
    raw_hashtags = out.get("hashtags") or []
    cleaned_hashtags = []
    for h in raw_hashtags:
        h = str(h).strip()
        if not h:
            continue
        if not h.startswith("#"):
            h = "#" + h
        # remove inner spaces
        h = "#" + re.sub(r"\s+", "", h.lstrip("#"))
        cleaned_hashtags.append(h.lower())
    if "#shorts" not in cleaned_hashtags:
        cleaned_hashtags.insert(0, "#shorts")
    niche_tag = f"#{niche.replace('_', '')}"
    if niche_tag not in cleaned_hashtags:
        cleaned_hashtags.append(niche_tag)
    out["hashtags"] = cleaned_hashtags[:5]

    # Word-count guard: log if model under-delivered (helps debug short videos)
    actual_words = len(out["script"].split())
    if actual_words < target_words * 0.7:
        log.warning("Script underweight: got %d words, wanted ~%d. "
                    "Hook=%r", actual_words, target_words, out["hook_overlay"])
    else:
        log.info("Script: %d words (target ~%d), hook=%r",
                 actual_words, target_words, out["hook_overlay"])
    return out


# ============================================================
# Visual concept extraction (for script-driven B-roll search)
# ============================================================

_VC_SYSTEM = """You convert a short voiceover script into VISUAL search queries
for stock-footage and TikTok hashtag search.

Return JSON only, with this exact shape:
{
  "concepts": [
    {"query": "short visual phrase", "alt": ["1-2 word variation", "another angle"]},
    ...
  ]
}

Rules:
- Generate exactly 5-7 concepts in narrative order (intro → body → outro).
- Each `query` is 2-5 words, lowercase, focused on a CONCRETE VISUAL
  (e.g. "athlete sprinting track", NOT abstract like "determination").
- Each `alt` array has 1-3 alternate phrasings to broaden the search
  (synonyms, different camera angles, related imagery).
- No hashtags, no brand names, no person names.
- Prefer cinematic / dramatic visuals that match a viral Short.
"""


def extract_visual_concepts(
    script_text: str,
    niche: str,
    nvidia_api_key: str,
    *,
    model: str = _DEFAULT_MODEL,
    fallback: Optional[list[str]] = None,
) -> list[dict]:
    """Use the LLM to derive 5-7 concrete visual search queries from `script_text`.

    Returns a list of dicts: [{"query": str, "alt": [str, ...]}, ...].
    On failure, returns a single concept built from `fallback` keywords.
    """
    if not script_text.strip():
        return [{"query": niche, "alt": fallback or []}]

    user_prompt = (
        f"Niche: {niche}\n\n"
        f"Voiceover script:\n\"\"\"\n{script_text.strip()[:1500]}\n\"\"\"\n\n"
        f"Output the JSON now."
    )
    try:
        client = OpenAI(api_key=nvidia_api_key, base_url=_NVIDIA_BASE_URL)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _VC_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.6,
            max_tokens=500,
        )
        data = _parse_json(resp.choices[0].message.content)
        concepts = data.get("concepts") or []
    except Exception as e:
        log.warning("extract_visual_concepts failed: %s", e)
        concepts = []

    cleaned: list[dict] = []
    for c in concepts:
        if not isinstance(c, dict):
            continue
        q = (c.get("query") or "").strip().lower()
        if not q:
            continue
        alts = [str(a).strip().lower() for a in (c.get("alt") or [])
                if a and str(a).strip()]
        cleaned.append({"query": q[:60], "alt": alts[:3]})
    if cleaned:
        return cleaned[:7]

    # Fallback: split keywords list into single-concept entries
    if fallback:
        return [{"query": k, "alt": []} for k in fallback[:5]]
    return [{"query": niche, "alt": []}]
