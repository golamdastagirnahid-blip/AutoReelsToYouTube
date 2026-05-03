# AutoReelsToYouTube

Fully automated, zero-cost pipeline that discovers viral Instagram Reels from individual creators and copyright-free sources, transforms them into polished YouTube Shorts using AI, and publishes them on a humanized schedule.

> **Status:** Foundation setup. Pipeline modules will be built stage-by-stage after setup is verified.

---

## What It Does

1. **Discovers** viral Reels from individual creators (skips corporate/brand accounts) + copyright-free sources (Pexels, Pixabay)
2. **Downloads** the video and extracts all metadata (creator, caption, hashtags)
3. **Analyzes** content using NVIDIA NIM AI (scene-by-scene understanding)
4. **Generates** an engaging, informative script matched to the video
5. **Creates voiceover** using ElevenLabs (Adam voice)
6. **Edits** the video: 4K sharpening, HDR look, saturation boost, subject tracking, hook in first 3s, background music from YouTube Audio Library
7. **Uploads** to YouTube with SEO-optimized title, tags, description, and owner credit + removal disclaimer
8. **Schedules** 6 videos/day at randomized human-like times

---

## Current Configuration

| Setting | Value |
|---|---|
| Niche | Multi-niche |
| Format | YouTube Shorts (vertical, <60s) |
| Language | English |
| Voiceover | ElevenLabs (paid 3rd-party API) — Adam voice |
| Sources | Instagram Reels + copyright-free (Pexels/Pixabay) |
| Hosting | GitHub Actions (free tier) |
| Cost | $0 (ElevenLabs already paid) |

---

## Tech Stack (All Free)

| Purpose | Tool |
|---|---|
| Automation runner | GitHub Actions |
| AI analysis + scripts | NVIDIA NIM API (free) |
| Voiceover | ElevenLabs (user-provided key) |
| Video download | yt-dlp / instaloader |
| Video editing | FFmpeg |
| Music | YouTube Audio Library |
| Storage | SQLite + GitHub repo |
| Upload | YouTube Data API v3 |

---

## Project Structure

```
AutoReelsToYouTube/
├── README.md                  # This file
├── SETUP.md                   # Step-by-step setup guide (START HERE)
├── requirements.txt           # Python dependencies
├── config.yaml                # Niches, schedule, editing settings
├── .gitignore                 # Keeps secrets safe
├── .env.example               # Template for local API keys
│
├── src/
│   ├── __init__.py
│   ├── main.py                # Pipeline orchestrator
│   ├── discovery.py           # Stage 1: Find viral reels
│   ├── downloader.py          # Stage 2: Download + metadata
│   ├── analyzer.py            # Stage 3: AI video analysis
│   ├── script_writer.py       # Stage 4: Script generation
│   ├── voiceover.py           # Stage 5: ElevenLabs TTS
│   ├── editor.py              # Stage 6: Video editing
│   ├── uploader.py            # Stage 7: YouTube upload
│   ├── scheduler.py           # Humanized posting schedule
│   └── utils/
│       ├── db.py              # SQLite tracker
│       ├── secrets.py         # Safe key loading
│       └── safety.py          # Copyright/duplicate checks
│
├── data/
│   ├── downloads/             # Raw reels (gitignored)
│   ├── processed/             # Edited videos (gitignored)
│   ├── music/                 # Background music library
│   └── tracker.db             # SQLite database (gitignored)
│
└── .github/
    └── workflows/
        └── autopublish.yml    # GitHub Actions schedule
```

---

## Security (Public Repo Safe)

All secrets live in **GitHub Secrets** — never in code.
See `SETUP.md` for exact steps.

Required secrets:
- `NVIDIA_API_KEY`
- `ELEVENLABS_API_KEY`
- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `YOUTUBE_REFRESH_TOKEN`
- `INSTAGRAM_SESSION` (optional)
- `OWNER_CONTACT_EMAIL` (for removal disclaimer)

---

## Important Disclaimers

- **Copyright risk:** Reposting others' Reels can still receive strikes even with credit. Start with copyright-free sources until channel is trusted.
- **Creator opt-out:** A blocklist of creators who request removal is maintained in `data/blocklist.txt`.
- **Terms of Service:** Instagram scraping may violate their ToS. Use responsibly.

---

## Next Step

Open **`SETUP.md`** and follow the checklist.
