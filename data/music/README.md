# Background Music

You have **two options** for supplying music — both fully automated downstream:

1. **Zero-effort: Jamendo auto-fetch** (recommended). Set
   `JAMENDO_CLIENT_ID` in your env and the pipeline fetches 5
   CC-licensed tracks per niche on the first run, caches them here,
   and credits the artist in every video's description automatically.
2. **Manual drop-in**. Download your own tracks from
   YouTube Audio Library / Pixabay / Mixkit and drop them into
   `data/music/<niche>/` — see folder layout below.

Both sources coexist: Jamendo only fetches when the folder has fewer
than 3 tracks, so anything you drop in manually is preserved and gets
picked at random along with the auto-fetched ones.

## Zero-effort: Jamendo auto-fetch

1. Create a free developer account at https://devportal.jamendo.com/
2. Click **Create new app** → copy the **client_id**
3. Add it to your env (local `.env` or GitHub Secret): `JAMENDO_CLIENT_ID=xxxxxxxx`
4. Next pipeline run, you'll see:
   ```
   INFO | src.music_fetcher | Jamendo: topping up music cache for 'motivation'…
   INFO | src.music_fetcher | Jamendo: downloaded ArtistName — Track Title (CC BY)
   ```
5. Artist credit is **automatically appended** to every YouTube
   description with the correct license + source URL (legally required
   by CC-BY / CC-SA):
   ```
   ────────────────────
   🎵 Music: "Track Title" by ArtistName
   License: CC BY (https://creativecommons.org/licenses/by/3.0/)
   Source: https://www.jamendo.com/track/1234567
   ```

Tag mapping per niche (from `config.yaml → niches[*].music_style`):

| Niche | Jamendo tag query | Vibe |
|---|---|---|
| motivation | `epic+cinematic+uplifting` | hero-theme, building tension |
| psychology_facts | `mysterious+ambient` | documentary, contemplative |
| tech_facts | `electronic+tech` | future-forward, synthwave |
| life_hacks | `upbeat+positive` | friendly, bouncy |
| fitness | `high-energy+rock+edm` | workout-ready |

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

## How the volume is controlled (you don't need to tune each track)

The editor uses **loudness normalization + sidechain ducking**, the same
technique Netflix, Spotify, and pro YouTube channels use. You can drop in
ANY Pixabay / YT-Audio-Library / Mixkit track — no matter how loud or quiet
it was mastered — and it will sit perfectly in the mix.

```yaml
editing:
  background_music:
    enabled: true
    target_lufs: -22         # music bed target (= 8 dB below VO)
    volume_db: -18           # legacy fallback
  transition_sfx:
    enabled: true
    volume_db: -8            # punchy whoosh on each crossfade
```

### Pipeline (fully automatic)

1. **VO** → loudnorm to **-14 LUFS** (YouTube broadcast standard)
2. **Music** → loudnorm to **-22 LUFS** (8 dB below VO, the pro music-bed
   spacing). A loud cinematic track and a quiet lo-fi track now sound
   *identical* in the mix.
3. **Sidechain ducking** → the instant VO is detected, the music is
   automatically compressed by ~8-12 dB (effective level ~-30 LUFS
   during speech). When VO pauses or finishes, the music swells back up
   to the -22 LUFS bed over 400 ms — that's the "professional radio feel".
4. **Transition SFX** → fixed **-8 dB** per whoosh, loud enough to punch
   through the mix for each crossfade.
5. **Final master** → everything is re-limited by the AAC encoder to
   **true peak -1.5 dBTP**, below YouTube's clipping threshold.

### If the music feels too loud or too quiet

Edit `target_lufs` in `config.yaml`:
- `-20` → **louder** music (more ambience, rock/edm niches)
- `-22` → **default** (balanced, recommended)
- `-25` → **quieter** music (documentary, meditation, whisper ASMR)

No need to re-master tracks or boost/cut in a DAW. The editor handles it.

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
