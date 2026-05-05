# Background Music — DISABLED

Background music has been removed from this pipeline. The audio track on
every rendered video is now:

  voiceover  +  transition SFX (whoosh on each crossfade)

## Why

Music ducking / sidechain compression introduced a class of ffmpeg
filter-graph bugs (label re-use, audio cutoffs, license attribution
overhead). Removing music makes the renderer dramatically more reliable
and removes any need for licensing / attribution sidecars.

## What if I drop tracks here?

They will be ignored. This folder is kept only so the path stays in git
and future re-introduction (if ever desired) is a one-flag change.

If you want music back, restore the `background_music` block in
`config.yaml` and the music-mixing branches in `src/editor.py`
(see git history for `_pick_music`, `_write_music_sidecar`,
`music_fetcher.py`).
