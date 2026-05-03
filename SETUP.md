# SETUP GUIDE — AutoReelsToYouTube

This is a step-by-step checklist for **non-developers**. Just follow it in order. Don't skip steps.

---

## Overview of What You'll Do

1. Install required software on your Windows PC
2. Create all the accounts & get free API keys
3. Put keys in GitHub Secrets (safe for public repo)
4. Do a **dry run** to test everything
5. Go live — start publishing

Estimated time: **2–3 hours** total (most of it is waiting for account approvals).

---

## PHASE 1 — Install Software on Your PC

### 1.1 Install Python 3.11+
- Go to https://www.python.org/downloads/
- Click **Download Python 3.12**
- Run installer
- **IMPORTANT:** Check the box **"Add Python to PATH"** before clicking Install
- Verify: open PowerShell → type `python --version` → should show `Python 3.12.x`

### 1.2 Install FFmpeg (for video editing)
- Go to https://www.gyan.dev/ffmpeg/builds/
- Under **release builds**, download **ffmpeg-release-essentials.zip**
- Extract to `C:\ffmpeg`
- Add `C:\ffmpeg\bin` to your Windows PATH:
  - Win key → search "Environment Variables" → open it
  - Click **Environment Variables** button
  - Under **System variables**, find **Path** → Edit → New → paste `C:\ffmpeg\bin`
  - OK → OK → OK
- Verify: open a **new** PowerShell → type `ffmpeg -version` → should print version info

### 1.3 Install Git
- Go to https://git-scm.com/download/win
- Install with all default options
- Verify: `git --version` in PowerShell

### 1.4 Install VS Code (optional but recommended)
- https://code.visualstudio.com/
- Makes editing config files easier

---

## PHASE 2 — Create Accounts & Get API Keys

Do these in order. Save every key in a temporary notepad — you'll paste them into GitHub Secrets later.

### 2.1 GitHub Account (if you don't have one)
- Sign up at https://github.com
- Verify your email
- Enable 2FA (Settings → Password and authentication)

### 2.2 NVIDIA NIM API Key (FREE)
- Go to https://build.nvidia.com
- Sign up with Google or email
- Search for any model (e.g. `glm-4.6` or `llama-3.1-70b`)
- Click **Get API Key** (top right)
- Copy the key that starts with `nvapi-...`
- **Save as:** `NVIDIA_API_KEY`

### 2.3 ElevenLabs API Key (you already have paid)
- Log in at https://elevenlabs.io
- Profile icon → **Profile + API key**
- Copy your API key (starts with `sk_...`)
- **Save as:** `ELEVENLABS_API_KEY`
- Voice ID for Adam: `pNInz6obpgDQGcFmaJgB` (already in config)

### 2.4 YouTube Data API v3 (FREE)
This is the longest step — take your time.

1. Go to https://console.cloud.google.com
2. Create a new project named **AutoReelsToYouTube**
3. Left menu → **APIs & Services → Library**
4. Search **YouTube Data API v3** → click → **Enable**
5. Left menu → **APIs & Services → OAuth consent screen**
   - User type: **External** → Create
   - App name: `AutoReelsToYouTube`
   - User support email: your email
   - Developer contact: your email
   - Save and continue through all screens
   - On **Test users** page → add your own Gmail
6. Left menu → **APIs & Services → Credentials**
   - **+ Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Name: `AutoReels`
   - Click Create
   - **Download JSON** and save it
   - From the JSON, copy:
     - `client_id` → **Save as:** `YOUTUBE_CLIENT_ID`
     - `client_secret` → **Save as:** `YOUTUBE_CLIENT_SECRET`
7. **Get refresh token** — I'll give you a one-time helper script to run. We'll do this in Phase 4.

### 2.5 Pexels API (FREE — for copyright-free videos)
- https://www.pexels.com/api/
- Sign up → Get API Key
- **Save as:** `PEXELS_API_KEY`

### 2.6 Pixabay API (FREE — backup source)
- https://pixabay.com/api/docs/
- Sign up → key is shown in docs page
- **Save as:** `PIXABAY_API_KEY`

### 2.7 (Optional) Telegram Bot — for pipeline notifications
- In Telegram, message **@BotFather** → `/newbot` → follow prompts
- Save the token → `TELEGRAM_BOT_TOKEN`
- Message your new bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` in browser
- Find `"chat":{"id":...}` → `TELEGRAM_CHAT_ID`

---

## PHASE 3 — Set Up the GitHub Repository

### 3.1 Create a PRIVATE Repository First
- Go to https://github.com/new
- Name: `AutoReelsToYouTube`
- **Private** (we'll make it public later after testing)
- Don't add README/gitignore (we already have them)
- Click Create

### 3.2 Push This Project to GitHub
Open PowerShell in the project folder `C:\Users\golam\CascadeProjects\AutoReelsToYouTube` and run:

```powershell
git init
git add .
git commit -m "Initial project setup"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/AutoReelsToYouTube.git
git push -u origin main
```

Replace `YOUR_USERNAME` with your GitHub username.

### 3.3 Add All Secrets to GitHub
1. On GitHub, open your repo
2. **Settings → Secrets and variables → Actions**
3. Click **New repository secret** for each of these (one at a time):

| Secret Name | Value |
|---|---|
| `NVIDIA_API_KEY` | from step 2.2 |
| `ELEVENLABS_API_KEY` | from step 2.3 |
| `ELEVENLABS_VOICE_ID` | `pNInz6obpgDQGcFmaJgB` |
| `YOUTUBE_CLIENT_ID` | from step 2.4 |
| `YOUTUBE_CLIENT_SECRET` | from step 2.4 |
| `YOUTUBE_REFRESH_TOKEN` | *(we'll add this in Phase 4)* |
| `PEXELS_API_KEY` | from step 2.5 |
| `PIXABAY_API_KEY` | from step 2.6 |
| `OWNER_CONTACT_EMAIL` | your email for removal disclaimer |
| `TELEGRAM_BOT_TOKEN` | optional |
| `TELEGRAM_CHAT_ID` | optional |

---

## PHASE 4 — Local Setup & YouTube Auth

### 4.1 Install Python Dependencies
In PowerShell, inside the project folder:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If `Activate.ps1` is blocked, run this once:
```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

### 4.2 Copy Environment File
```powershell
Copy-Item .env.example .env
```
Open `.env` in VS Code and paste your keys for **local testing**.
(This file is gitignored — will never be committed.)

### 4.3 Get YouTube Refresh Token
I'll build a helper script `src/utils/get_youtube_token.py` in the next message.
You'll run it once, sign in with your YouTube Google account, and it prints your `YOUTUBE_REFRESH_TOKEN`.
Then you add that to GitHub Secrets.

---

## PHASE 5 — Dry Run (Test Mode)

The config has `dry_run: true` by default → no actual uploads, just test the pipeline.

```powershell
python -m src.main --niche motivation --count 1
```

Expected output:
- Finds 1 trending reel
- Downloads it
- Analyzes it
- Writes a script
- Generates voiceover
- Edits the video
- **Saves output to `data/processed/` instead of uploading**

Check the output video manually before going live.

---

## PHASE 6 — Go Live

1. Edit `config.yaml` → set `dry_run: false`
2. Commit and push
3. GitHub Actions will start running on schedule (6 videos/day)
4. Watch the **Actions** tab on GitHub for logs
5. Watch your YouTube channel for new uploads

---

## Build Progress (what's done vs pending)

- [x] Project scaffold + README + config + .gitignore
- [x] Requirements file
- [x] Setup guide
- [ ] `src/utils/secrets.py` — safe key loader
- [ ] `src/utils/db.py` — SQLite tracker
- [ ] `src/discovery.py` — Instagram reel finder
- [ ] `src/downloader.py` — yt-dlp wrapper
- [ ] `src/analyzer.py` — NVIDIA NIM video analysis
- [ ] `src/script_writer.py` — AI script generator
- [ ] `src/voiceover.py` — ElevenLabs TTS
- [ ] `src/editor.py` — FFmpeg video editor
- [ ] `src/uploader.py` — YouTube uploader
- [ ] `src/scheduler.py` — humanized scheduling
- [ ] `src/main.py` — pipeline orchestrator
- [ ] `.github/workflows/autopublish.yml` — scheduled Actions
- [ ] `src/utils/get_youtube_token.py` — one-time auth helper

---

## What To Do RIGHT NOW

**Start with Phase 1 and Phase 2.**
Message me when you've:
1. Installed Python + FFmpeg + Git
2. Got your `NVIDIA_API_KEY`
3. Got your Google Cloud `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET`

Then I'll build the code modules one by one, starting with the YouTube token helper.
