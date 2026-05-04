# Background Music

Drop royalty-free MP3 / WAV tracks here. The editor picks one at random per video.

## Folder layout

```
data/music/
├── motivation/         # niche-specific tracks (preferred)
│   ├── epic_uplifting.mp3
│   └── cinematic_drive.mp3
├── tech_facts/
│   └── electronic_pulse.mp3
└── *.mp3               # fallback for any niche
```

The editor first looks in `data/music/<niche>/`. If empty, it falls back
to anything directly inside `data/music/`. If both are empty, the video
is rendered with VO + transition SFX only (still ships fine).

## Where to get tracks (all free, all safe for YouTube monetization)

| Source | Link | Login | Notes |
|---|---|---|---|
| **YouTube Audio Library** | studio.youtube.com → Audio Library | Yes | Best — explicitly licensed for monetized YouTube |
| **Pixabay Music** | https://pixabay.com/music/ | No | Search e.g. "motivational cinematic", click Download |
| **Mixkit** | https://mixkit.co/free-stock-music/ | No | Curated, no attribution required |
| **Free Music Archive** | https://freemusicarchive.org/ | No | Filter by CC0 / CC-BY |
| **Uppbeat** | https://uppbeat.io/ | Yes (free) | Free tier requires credit in description |

## Recommended levels (already set in `config.yaml`)

```yaml
editing:
  background_music:
    volume_db: -18           # under voiceover
  transition_sfx:
    enabled: true
    volume_db: -8            # punchy whoosh on each crossfade
```

These match what professional faceless YouTubers use:
- **VO** at 0 dB (loudnorm to -14 LUFS, YouTube broadcast standard)
- **Music** at -18 dB, sidechain-ducked when VO speaks (auto-handled)
- **SFX** at -8 dB (so transitions punch through without being painful)
- **Final master** loudnorm-ed to -14 LUFS, true peak -1.5 dBTP

## Suggested first-day playlist (pick 2-3 per niche)

For **motivation / fitness**:
- "Epic Cinematic" / "Hero Theme" type tracks with rising tension
- Search Pixabay for: `motivational epic`, `cinematic drive`, `inspiring action`

For **psychology_facts / tech_facts**:
- Ambient electronic, mysterious, lo-fi pulse
- Search Pixabay for: `mysterious ambient`, `tech corporate`, `documentary`

For **life_hacks**:
- Upbeat, friendly, light-hearted
- Search Pixabay for: `upbeat positive`, `acoustic happy`, `cooking show`

3-5 tracks per niche is enough — the random picker means you won't hear
the same one twice in a row across 6 videos/day.
