"""Stage 6 — Video editing pipeline (FFmpeg only, no paid tools).

Two render modes:
  - Single-clip:    one source video, optional Ken Burns zoom + grading.
  - Multi-clip:     2-8 source clips, smart segment selection, crossfade
                    transitions, then grading + captions on the stitched master.

Common stages (both modes):
  1. Trim / scale / crop to vertical 1080x1920 (Lanczos)
  2. Cinematic colour grade: warm tone, S-curve, vignette, sharpen
  3. Mute original audio; audio track = voiceover + transition SFX (no music)
  4. Bold animated hook overlay during the first 3 seconds
  5. Karaoke captions burned in via libass
  6. CRF 18 / preset slow H.264 + 192 kbps AAC at 48 kHz
"""
from __future__ import annotations

import logging
import random
import subprocess
from pathlib import Path
from typing import Optional

from src.segments import pick_best_segment, probe_duration

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROCESSED_DIR = _PROJECT_ROOT / "data" / "processed"
SEGMENTS_DIR = PROCESSED_DIR / "segments"
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
SEGMENTS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# Filter graph builder
# ============================================================

def _video_filter_chain(saturation: float, sharpen: bool, hdr_look: bool,
                        target_w: int, target_h: int,
                        zoom_pan: bool = True,
                        denoise: bool = True) -> str:
    """Cinematic filter chain: denoise → scale→crop → grade → zoom → sharpen.

    Filter order matters — grading first, then sharpening last keeps edges crisp
    without amplifying noise.
    """
    parts: list[str] = []

    # Pre-denoise (lightweight) — cleans social-media compression artifacts
    if denoise:
        parts.append("hqdn3d=1.5:1.5:6:6")

    # Lanczos for high-quality upscale of low-res sources, then center-crop
    parts.append(
        f"scale={target_w}:{target_h}:flags=lanczos:force_original_aspect_ratio=increase,"
        f"crop={target_w}:{target_h}"
    )

    # Subtle Ken Burns zoom — 6% zoom over the clip duration
    # Using zoompan with d=1 frame style won't work; instead use scale+pad approach.
    # Simpler: gentle constant zoom via crop with shrinking box.
    if zoom_pan:
        # 1.00 → 1.06 over time using `t` variable inside crop is unreliable;
        # use minterpolate-friendly steady micro-zoom via scale/zoom-pan filter.
        parts.append(
            f"zoompan=z='min(zoom+0.0008,1.06)':d=1:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"s={target_w}x{target_h}:fps=30"
        )

    # Cinematic colour grade:
    #   - Lift saturation slightly
    #   - Boost contrast
    #   - Mild gamma to lift midtones
    #   - Slight warm tone (red+, blue-) for filmic feel
    parts.append(
        f"eq=saturation={saturation:.2f}:contrast=1.10:brightness=0.02:gamma=1.05:gamma_r=1.02:gamma_b=0.98"
    )

    if hdr_look:
        # Pseudo-HDR: tone-map style S-curve + selective shadow lift
        parts.append("curves=master='0/0 0.25/0.18 0.5/0.55 0.75/0.85 1/1'")

    # Subtle vignette for cinematic framing
    parts.append("vignette=PI/5")

    # Sharpen LAST so it doesn't amplify noise
    if sharpen:
        parts.append("unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount=1.0"
                     ":chroma_msize_x=3:chroma_msize_y=3:chroma_amount=0.5")

    # Final output format
    parts.append("format=yuv420p")
    return ",".join(parts)


def _wrap_hook_text(text: str, max_chars_per_line: int = 14) -> str:
    """Wrap a short hook string onto at most 2 lines so it never overflows
    horizontally on a 1080-wide vertical frame.

    Strategy: greedy fit by words. Returns the original text if it already fits.
    """
    if not text:
        return ""
    if len(text) <= max_chars_per_line:
        return text
    words = text.split()
    line1, line2 = [], []
    cur = line1
    for w in words:
        joined = " ".join(cur + [w])
        if len(joined) <= max_chars_per_line:
            cur.append(w)
        else:
            if cur is line1 and not line2:
                cur = line2
                cur.append(w)
            else:
                # Even line 2 overflowed — drop the rest (we already truncated upstream)
                break
    if not line2:
        return " ".join(line1)
    return " ".join(line1) + "\n" + " ".join(line2)


def _hook_drawtext(hook_text: str, duration: float = 3.0,
                   font_size: int = 92) -> str:
    """Bold animated hook text for the first `duration` seconds.

    Renders SAFE — auto-wraps to 2 lines, sized to never run off-frame on
    1080-wide phones. Positioned at 18% from top (clear of any phone notch /
    status bar overlay on Shorts).
    """
    raw = (hook_text or "").strip().upper()
    if not raw:
        return ""
    wrapped = _wrap_hook_text(raw, max_chars_per_line=14)
    # ffmpeg drawtext: ' is special, : is special, \n is real newline.
    # Use 'text=' with quoted value — wrap newline as %{eif:...} is overkill,
    # easier to use the `text` arg with literal `\n` which drawtext renders.
    safe = wrapped.replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")
    return (
        f"drawtext=text='{safe}'"
        f":fontcolor=white:fontsize={font_size}:borderw=6:bordercolor=black"
        f":shadowx=3:shadowy=3:shadowcolor=black@0.7"
        f":line_spacing=10"
        f":x=(w-text_w)/2"
        # 18% from top dodges phone notch + Shorts top UI bar
        f":y='h*0.18 + 30*max(0,1-(t/0.4))'"
        f":alpha='if(lt(t,0.2),t/0.2,if(gt(t,{duration}-0.3),max(0,({duration}-t)/0.3),1))'"
        f":enable='between(t,0,{duration})'"
    )


# ============================================================
# Procedural transition swooshes (no external SFX files needed)
# ============================================================

SFX_DIR = _PROJECT_ROOT / "data" / "sfx"
SFX_DIR.mkdir(parents=True, exist_ok=True)

# 4 distinct swoosh "characters" — synthesized once, reused forever.
# Each is brown-noise + bandpass envelope, tuned for a different feel.
_SWOOSH_RECIPES = [
    # (name, duration, bp_freq, bp_width, attack, release, gain)
    ("whoosh_high",  0.45, 1800, 2200, 0.04, 0.18, 0.55),  # bright fast cut
    ("whoosh_mid",   0.55, 1200, 1800, 0.05, 0.22, 0.60),  # classic crossfade
    ("whoosh_low",   0.65,  700, 1500, 0.06, 0.30, 0.65),  # dramatic / impact
    ("whoosh_short", 0.30, 1500, 2000, 0.03, 0.12, 0.50),  # snap / quick cut
]


def _ensure_swooshes() -> list[Path]:
    """Synthesize the swoosh library on first call. Cached as WAV files."""
    out: list[Path] = []
    for name, dur, freq, width, attack, release, gain in _SWOOSH_RECIPES:
        path = SFX_DIR / f"{name}.wav"
        if path.exists() and path.stat().st_size > 1000:
            out.append(path)
            continue
        # Brown-noise burst → bandpass → fast attack / longer release
        af = (
            f"bandpass=f={freq}:width_type=h:w={width},"
            f"afade=t=in:st=0:d={attack},"
            f"afade=t=out:st={dur - release:.3f}:d={release},"
            f"volume={gain}"
        )
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi",
                 "-i", f"anoisesrc=color=brown:d={dur}:sample_rate=48000:amplitude=0.6",
                 "-af", af,
                 "-ac", "2", "-ar", "48000",
                 str(path)],
                capture_output=True, check=True, timeout=15,
            )
            out.append(path)
        except subprocess.CalledProcessError as e:
            log.warning("Swoosh synthesis failed for %s: %s",
                        name, e.stderr.decode("utf-8", errors="ignore")[:200])
        except Exception as e:
            log.warning("Swoosh synthesis error for %s: %s", name, e)
    return [p for p in out if p.exists()]


def _build_sfx_audio_chain(transition_times: list[float],
                           total_duration: float,
                           sfx_volume_db: float = -8.0,
                           start_input_idx: int = 0) -> tuple[str, list[Path], str]:
    """Build a filter_complex fragment that mixes swooshes at each
    `transition_times` timestamp into a single mono/stereo stream.

    Returns:
      - filter fragment ending with [sfx_mix] label (or empty if no SFX),
      - list of input files (to be added with -i),
      - the output stream label "[sfx_mix]" or "" if no SFX.
    """
    swooshes = _ensure_swooshes()
    if not swooshes or not transition_times:
        return "", [], ""
    chosen: list[tuple[Path, float]] = []
    rng = random.Random(int(total_duration * 1000))   # deterministic per video
    for t in transition_times:
        chosen.append((rng.choice(swooshes), t))
    # Each swoosh becomes its own input, delayed by its timestamp
    pieces = []
    files: list[Path] = []
    for k, (path, when) in enumerate(chosen):
        idx = start_input_idx + k
        delay_ms = int(max(0.0, when) * 1000)
        # adelay applied to both channels: "ms|ms"
        pieces.append(
            f"[{idx}:a]volume={sfx_volume_db}dB,"
            f"adelay={delay_ms}|{delay_ms},"
            f"apad=whole_dur={total_duration:.3f}[sfx{k}]"
        )
        files.append(path)
    mix_inputs = "".join(f"[sfx{k}]" for k in range(len(chosen)))
    pieces.append(
        f"{mix_inputs}amix=inputs={len(chosen)}:duration=first:"
        f"dropout_transition=0:normalize=0[sfx_mix]"
    )
    return ";".join(pieces), files, "[sfx_mix]"


# ============================================================
# Public API
# ============================================================

def _ffmpeg_escape_path(path: Path) -> str:
    """Escape a path for use inside an FFmpeg filter argument.

    On Windows, the colon in 'C:\\foo' breaks the filter parser unless escaped.
    Forward-slashes work cross-platform.
    """
    p = str(path).replace("\\", "/")
    # In libavfilter, colons separate filter options, so escape them.
    p = p.replace(":", r"\:")
    return p


def edit_video(
    *,
    source_video: Path,
    voiceover_audio: Path,
    video_id: int,
    niche: str,
    hook_text: str,
    target_resolution: tuple[int, int] = (1080, 1920),
    saturation: float = 1.25,
    sharpen: bool = True,
    hdr_look: bool = True,
    captions_ass: Optional[Path] = None,
    fonts_dir: Optional[Path] = None,
    zoom_pan: bool = True,
) -> Optional[Path]:
    """Render the final Short from a single source clip."""
    out = PROCESSED_DIR / f"{video_id}_final.mp4"
    if out.exists():
        out.unlink()

    # Probe the voiceover duration so we can force the final video to match
    # exactly — preventing the audio from being clipped if the source video
    # is shorter than the VO. Add a 1.0s tail so the video holds on the
    # last frame for a full second of natural silence after the script
    # finishes. Looks far more professional than a hard cut, and absorbs
    # loudnorm's 2-pass timing drift so the last syllable is never clipped.
    from src.voiceover import audio_duration
    raw_vo_dur = audio_duration(voiceover_audio) or 30.0
    vo_dur = raw_vo_dur + 1.0

    target_w, target_h = target_resolution
    vf = _video_filter_chain(saturation, sharpen, hdr_look, target_w, target_h,
                             zoom_pan=zoom_pan)
    hook = _hook_drawtext(hook_text)
    if hook:
        vf = f"{vf},{hook}"

    # Burn karaoke captions on top of everything else
    if captions_ass and captions_ass.exists():
        sub_arg = _ffmpeg_escape_path(captions_ass)
        sub_filter = f"subtitles='{sub_arg}'"
        if fonts_dir and fonts_dir.exists() and any(fonts_dir.iterdir()):
            sub_filter += f":fontsdir='{_ffmpeg_escape_path(fonts_dir)}'"
        vf = f"{vf},{sub_filter}"

    # Hold the last frame for up to 4s if needed so the video stream is never
    # shorter than the VO. Combined with -t below, both streams end at the
    # same point and the audio is never truncated.
    vf = f"{vf},tpad=stop_mode=clone:stop_duration=4"

    # ----------------------------------------------------------------
    # Audio: voiceover ONLY (background music removed by user request).
    # The VO chain cleans up sub-rumble, applies mild compression for
    # consistent loudness, and normalises to YouTube's -14 LUFS target.
    # apad ensures the audio stream is exactly vo_dur long (= VO + 1.0s
    # tail) so the final render never has silent dead air or chopped
    # last-syllable artefacts.
    # ----------------------------------------------------------------
    cmd = ["ffmpeg", "-y", "-i", str(source_video), "-i", str(voiceover_audio)]
    vo_chain = (
        "highpass=f=80,"
        "acompressor=threshold=-20dB:ratio=3:attack=10:release=200,"
        "loudnorm=I=-14:TP=-1.5:LRA=11"
    )
    apad = f"apad=whole_dur={vo_dur:.3f}"
    cmd += [
        "-filter_complex", f"[1:a]{vo_chain},{apad}[a]",
        "-map", "0:v", "-map", "[a]",
    ]

    cmd += [
        "-vf", vf,
        "-r", "30",
        # Visually lossless H.264, broadcast-quality settings
        "-c:v", "libx264",
        "-preset", "slow",          # slower = better compression/quality
        "-crf", "18",                # 18 = visually lossless
        "-profile:v", "high",
        "-level", "4.2",
        "-pix_fmt", "yuv420p",
        "-tune", "film",             # better detail preservation for real footage
        "-x264-params",
        "keyint=60:min-keyint=60:scenecut=0",  # smooth seeking
        # AAC 192 kbps stereo — YouTube's recommended audio for Shorts
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        # Force EXACT duration (= VO length). Replaces -shortest, which would
        # truncate the VO whenever the source clip was shorter than the audio.
        "-t", f"{vo_dur:.3f}",
        "-movflags", "+faststart",
        str(out),
    ]

    log.info("Single-clip render: total %.2fs → %s", vo_dur, out.name)
    try:
        subprocess.run(cmd, capture_output=True, check=True)
    except subprocess.CalledProcessError as e:
        full_err = e.stderr.decode("utf-8", errors="ignore")
        log.error("FFmpeg failed (tail):\n%s", full_err[-2000:])
        return None
    if out.exists():
        return out
    return None


# ============================================================
# Multi-clip composer
# ============================================================

# A curated palette of cinematic transitions that look great in vertical Shorts.
# We intentionally exclude flashy cheesy ones like pixelize / spiralopen.
_TRANSITION_POOL = [
    "fade", "fadeblack", "fadewhite",
    "slideup", "slidedown",
    "smoothleft", "smoothright",
    "wipeleft", "wiperight",
    "circleopen", "dissolve",
    "hblur", "radial",
]


def _normalize_segment(
    src: Path,
    out: Path,
    *,
    start: float,
    duration: float,
    target_w: int,
    target_h: int,
    fps: int = 30,
) -> Optional[Path]:
    """Trim, scale & crop one source clip to a uniform (w, h, fps, codec).

    Output is intermediate (no grading yet) — we grade after concatenation
    so the look is consistent across clips even if sources differ wildly.
    """
    if out.exists():
        out.unlink()
    vf = (
        f"scale={target_w}:{target_h}:flags=lanczos:"
        f"force_original_aspect_ratio=increase,"
        f"crop={target_w}:{target_h},"
        f"setsar=1,fps={fps},format=yuv420p"
    )
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-i", str(src),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-an",                              # drop original audio
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out),
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=120)
    except subprocess.CalledProcessError as e:
        log.warning("Segment normalize failed for %s: %s", src.name,
                    e.stderr.decode("utf-8", errors="ignore")[:500])
        return None
    except subprocess.TimeoutExpired:
        log.warning("Segment normalize timed out for %s", src.name)
        return None
    return out if out.exists() else None


def _build_xfade_chain(
    n_clips: int, seg_duration: float, fade_duration: float
) -> tuple[str, str]:
    """Build the xfade filter chain for `n_clips` segments.

    Returns (filter_complex_string, last_label).
    Each crossfade overlaps the previous clip by `fade_duration`.
    """
    parts: list[str] = []
    # Setup each input: ensure same SAR and starting PTS at 0
    for i in range(n_clips):
        parts.append(f"[{i}:v]setpts=PTS-STARTPTS,format=yuv420p[v{i}]")
    cur_label = "v0"
    cumulative = seg_duration
    transitions = list(_TRANSITION_POOL)
    random.shuffle(transitions)
    for i in range(1, n_clips):
        next_label = f"vx{i}"
        offset = cumulative - fade_duration
        # cycle through palette so consecutive transitions differ
        trans = transitions[(i - 1) % len(transitions)]
        parts.append(
            f"[{cur_label}][v{i}]xfade=transition={trans}:"
            f"duration={fade_duration:.3f}:offset={offset:.3f}[{next_label}]"
        )
        cur_label = next_label
        cumulative += seg_duration - fade_duration
    return ";".join(parts), cur_label


def edit_video_multiclip(
    *,
    source_videos: list[Path],
    voiceover_audio: Path,
    video_id: int,
    niche: str,
    hook_text: str,
    target_resolution: tuple[int, int] = (1080, 1920),
    saturation: float = 1.25,
    sharpen: bool = True,
    hdr_look: bool = True,
    captions_ass: Optional[Path] = None,
    fonts_dir: Optional[Path] = None,
    seg_duration: Optional[float] = None,
    fade_duration: float = 0.4,
    target_total_duration: Optional[float] = None,
    sfx_enabled: bool = True,
    sfx_volume_db: float = -8.0,
) -> Optional[Path]:
    """Compose a final Short from MULTIPLE source clips with crossfade transitions.

    Steps:
      1. For each source: detect best segment via scene-cut/motion heuristic
      2. Normalize each segment to target resolution, fps, codec
      3. Stitch them together with xfade transitions in filter_complex
      4. Apply colour grade + hook + captions on top
      5. Render audio: VO + transition SFX (no background music)

    `target_total_duration` should equal the VO duration (so video and audio
    line up). `seg_duration` defaults to filling that duration evenly.
    """
    sources = [Path(p) for p in source_videos if Path(p).exists()]
    if not sources:
        log.error("No source clips provided to multiclip editor")
        return None
    if len(sources) == 1:
        log.info("Only one source clip — falling back to single-clip mode")
        return edit_video(
            source_video=sources[0],
            voiceover_audio=voiceover_audio,
            video_id=video_id,
            niche=niche,
            hook_text=hook_text,
            target_resolution=target_resolution,
            saturation=saturation, sharpen=sharpen, hdr_look=hdr_look,
            captions_ass=captions_ass, fonts_dir=fonts_dir,
            zoom_pan=True,
        )

    target_w, target_h = target_resolution
    n = len(sources)

    # Auto-derive segment length to match VO duration evenly
    if target_total_duration and target_total_duration > 0:
        # Total = n*seg - (n-1)*fade  →  seg = (Total + (n-1)*fade) / n
        seg_duration = (target_total_duration + (n - 1) * fade_duration) / n
    if not seg_duration:
        seg_duration = 4.0
    # Clamp to viewer-friendly range (engaging pacing without feeling jumpy).
    # Upper bound used to be 7.0 but that truncated VOs longer than ~33s with
    # 5 segments. We bump it so the segment math can always fit the VO.
    seg_duration = max(2.5, min(12.0, seg_duration))

    # 1+2. Pick best segments and normalize
    segs: list[Path] = []
    for idx, src in enumerate(sources):
        sel = pick_best_segment(src, target_duration=seg_duration)
        if not sel:
            log.warning("Skipping %s: no segment selected", src.name)
            continue
        start, dur = sel
        # If clip is shorter than seg_duration, dur will be < seg_duration.
        # Use the shorter value so we don't run past EOF.
        eff_dur = min(dur, seg_duration)
        seg_out = SEGMENTS_DIR / f"{video_id}_seg{idx:02d}.mp4"
        normalized = _normalize_segment(
            src, seg_out,
            start=start, duration=eff_dur,
            target_w=target_w, target_h=target_h,
        )
        if normalized:
            segs.append(normalized)

    if len(segs) < 2:
        log.warning("Multiclip needs >=2 normalized segs, got %d — falling back",
                    len(segs))
        if segs:
            return edit_video(
                source_video=segs[0],
                voiceover_audio=voiceover_audio,
                video_id=video_id, niche=niche, hook_text=hook_text,
                target_resolution=target_resolution,
                saturation=saturation, sharpen=sharpen, hdr_look=hdr_look,
                captions_ass=captions_ass, fonts_dir=fonts_dir,
                zoom_pan=True,
            )
        return None

    n_used = len(segs)

    # 3. Stitch with xfade
    xfade_filter, last_label = _build_xfade_chain(
        n_used, seg_duration, fade_duration
    )

    # 4. Final grading + hook + captions on stitched master.
    # We disable Ken Burns zoom because the xfade transitions already provide
    # plenty of motion variety.
    grade_chain = _video_filter_chain(
        saturation, sharpen, hdr_look, target_w, target_h, zoom_pan=False,
    )
    hook = _hook_drawtext(hook_text)
    extra = grade_chain
    if hook:
        extra = f"{extra},{hook}"
    if captions_ass and captions_ass.exists():
        sub_arg = _ffmpeg_escape_path(captions_ass)
        sub_filter = f"subtitles='{sub_arg}'"
        if fonts_dir and fonts_dir.exists() and any(fonts_dir.iterdir()):
            sub_filter += f":fontsdir='{_ffmpeg_escape_path(fonts_dir)}'"
        extra = f"{extra},{sub_filter}"

    # tpad pads the video stream by holding the last frame for up to 4s,
    # guaranteeing the video is never shorter than the VO. We then trim both
    # streams to exactly `total_dur` via the -t flag below.
    final_video_filter = (
        f"[{last_label}]{extra},"
        f"tpad=stop_mode=clone:stop_duration=4"
        f"[vfinal]"
    )
    full_filter = f"{xfade_filter};{final_video_filter}"

    # ---------------------------------------------------------------
    # 5. Audio: VO + transition SFX (background music removed entirely)
    # ---------------------------------------------------------------
    # The audio graph is now: VO -> processing chain -> [vo_processed]
    # then optionally amix'd with SFX swooshes. Total length is forced to
    # `total_dur` via apad so the audio ends EXACTLY when the video ends.
    vo_chain = (
        "highpass=f=80,"
        "acompressor=threshold=-20dB:ratio=3:attack=10:release=200,"
        "loudnorm=I=-14:TP=-1.5:LRA=11"
    )
    vo_idx = n_used                # VO is the (n_used)th input

    # Compute crossfade timestamps for the SFX mix.
    transition_times: list[float] = []
    if sfx_enabled:
        for i in range(1, n_used):
            # nudge -50ms so the swoosh PEAKS at the crossfade midpoint
            t = i * (seg_duration - fade_duration) - 0.05
            transition_times.append(max(0.05, t))

    total_dur = (target_total_duration
                 or (n_used * seg_duration - (n_used - 1) * fade_duration))

    sfx_start_idx = vo_idx + 1
    sfx_filter, sfx_files, sfx_label = _build_sfx_audio_chain(
        transition_times, total_dur,
        sfx_volume_db=sfx_volume_db,
        start_input_idx=sfx_start_idx,
    )

    # Build audio filter graph (VO + optional SFX, no music)
    apad = f"apad=whole_dur={total_dur:.3f}"
    if sfx_filter:
        # VO processed -> [vop]; mix with SFX -> [a]
        afilter = (
            f"[{vo_idx}:a]{vo_chain}[vop];"
            f"{sfx_filter};"
            f"[vop]{sfx_label}amix=inputs=2:"
            f"duration=first:dropout_transition=0:normalize=0,"
            f"{apad}[a]"
        )
    else:
        # VO only — single chain, no labels needed
        afilter = f"[{vo_idx}:a]{vo_chain},{apad}[a]"

    full_filter = f"{full_filter};{afilter}"

    # Build ffmpeg command (segments + VO + SFX inputs; NO music input)
    cmd = ["ffmpeg", "-y"]
    for seg in segs:
        cmd += ["-i", str(seg)]
    cmd += ["-i", str(voiceover_audio)]
    for sfx in sfx_files:
        cmd += ["-i", str(sfx)]

    out = PROCESSED_DIR / f"{video_id}_final.mp4"
    if out.exists():
        out.unlink()

    cmd += [
        "-filter_complex", full_filter,
        "-map", "[vfinal]", "-map", "[a]",
        "-r", "30",
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", "18",
        "-profile:v", "high",
        "-level", "4.2",
        "-pix_fmt", "yuv420p",
        "-tune", "film",
        "-x264-params", "keyint=60:min-keyint=60:scenecut=0",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        # Force EXACT total duration. Replaces -shortest, which used to cut
        # the VO whenever the visual stream was shorter than the audio. The
        # tpad filter above ensures the video reaches at least total_dur, and
        # this -t makes both streams end at exactly total_dur.
        "-t", f"{total_dur:.3f}",
        "-movflags", "+faststart",
        str(out),
    ]

    log.info("Multiclip render: %d segments × %.2fs (fade %.2fs, total %.2fs) → %s",
             n_used, seg_duration, fade_duration, total_dur, out.name)
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=600)
    except subprocess.CalledProcessError as e:
        # Log the LAST 3000 chars (where ffmpeg's actual error message lives)
        # rather than the FIRST 3000 (which is just the verbose banner).
        full_err = e.stderr.decode("utf-8", errors="ignore")
        log.error("Multiclip ffmpeg failed (tail):\n%s", full_err[-3000:])
        return None
    except subprocess.TimeoutExpired:
        log.error("Multiclip render timed out")
        return None
    if out.exists():
        return out
    return None
