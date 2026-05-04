"""Karaoke-style word-by-word captions.

Pipeline:
  1. Transcribe the voiceover MP3 with faster-whisper (word-level timestamps)
  2. Group into 1-2 word chunks (the viral TikTok/Shorts style)
  3. Render as ASS subtitle file with big bold style + pop animation
  4. Editor burns it into the video with FFmpeg's libass

Style is configurable via config.yaml -> editing.captions.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAPTION_DIR = _PROJECT_ROOT / "data" / "processed"
CAPTION_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class Word:
    text: str
    start: float
    end: float


# ============================================================
# 1. Transcription (faster-whisper)
# ============================================================

def transcribe_words(audio_path: Path, language: str = "en",
                     model_size: str = "tiny.en") -> list[Word]:
    """Transcribe audio into word-level timestamps.

    `tiny.en` (~40MB) is plenty for forced alignment when you already know
    the spoken text comes from clean TTS output. Use `base.en` (~150MB) for
    extra accuracy on multilingual content.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log.error("faster-whisper not installed — captions disabled")
        return []

    try:
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments, _info = model.transcribe(
            str(audio_path),
            language=language if language != "auto" else None,
            word_timestamps=True,
            vad_filter=True,
            beam_size=1,
        )
        words: list[Word] = []
        for seg in segments:
            for w in (seg.words or []):
                txt = (w.word or "").strip()
                if not txt:
                    continue
                words.append(Word(text=txt, start=float(w.start), end=float(w.end)))
        log.info("Transcribed %d words from %s", len(words), audio_path.name)
        return words
    except Exception as e:
        log.error("Whisper transcription failed: %s", e)
        return []


# ============================================================
# 2. Chunk grouping (1-2 words for max readability)
# ============================================================

def group_words(words: list[Word], max_words: int = 2,
                max_chars: int = 14) -> list[list[Word]]:
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    cur_chars = 0
    for w in words:
        wlen = len(w.text)
        if cur and (len(cur) >= max_words or cur_chars + wlen + 1 > max_chars):
            chunks.append(cur)
            cur = [w]
            cur_chars = wlen
        else:
            cur.append(w)
            cur_chars += wlen + 1
    if cur:
        chunks.append(cur)
    return chunks


# ============================================================
# 3. ASS file generation
# ============================================================

def _ts(seconds: float) -> str:
    """ASS timestamp: H:MM:SS.cs (centiseconds)."""
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - (h * 3600) - (m * 60)
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _ass_color(hex_rgb: str) -> str:
    """#RRGGBB or RRGGBB -> &H00BBGGRR (ASS BGR with leading 00 alpha)."""
    h = hex_rgb.lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


def _escape_ass_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def build_ass(
    words: list[Word],
    out_path: Path,
    *,
    video_width: int = 1080,
    video_height: int = 1920,
    font_name: str = "Impact",
    font_size: int = 96,
    primary_hex: str = "#FFFFFF",
    outline_hex: str = "#000000",
    highlight_hex: str = "#FFEB3B",
    outline_px: int = 6,
    margin_v: int = 520,
    pop_animation: bool = True,
    uppercase: bool = True,
    max_words_per_chunk: int = 2,
    max_chars_per_chunk: int = 14,
) -> Optional[Path]:
    """Generate ASS subtitle file. Returns the path or None if no words."""
    if not words:
        return None

    primary = _ass_color(primary_hex)
    outline = _ass_color(outline_hex)
    highlight = _ass_color(highlight_hex)  # reserved for future per-word style

    chunks = group_words(words, max_words=max_words_per_chunk,
                         max_chars=max_chars_per_chunk)

    # ASS BackColour with high alpha gives a soft "glow" via thicker shadow
    # BorderStyle 1 = outline + drop shadow
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        f"PlayResX: {video_width}\n"
        f"PlayResY: {video_height}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # Bigger outline (8px) and a soft drop shadow (4px) for visibility on any background
        f"Style: Pop,{font_name},{font_size},{primary},{highlight},{outline},"
        f"&H80000000,1,0,0,0,100,100,2,0,1,{outline_px},4,2,40,40,{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
    )

    lines = [header]
    for chunk in chunks:
        start = chunk[0].start
        end = max(chunk[-1].end, start + 0.15)
        text = " ".join(w.text for w in chunk)
        text = re.sub(r"\s+", " ", text).strip(" ,.!?\"'")
        if uppercase:
            text = text.upper()
        text = _escape_ass_text(text)

        # Pop-in: scale 80% -> 105% over 100ms, then settle to 100% by 200ms
        if pop_animation:
            effect = (
                r"{\an2\fad(50,80)"
                r"\t(0,100,\fscx105\fscy105)"
                r"\t(100,200,\fscx100\fscy100)"
                r"\fscx80\fscy80}"
            )
        else:
            effect = r"{\an2\fad(50,50)}"

        lines.append(
            f"Dialogue: 0,{_ts(start)},{_ts(end)},Pop,,0,0,0,,{effect}{text}"
        )

    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Wrote %d caption chunks -> %s", len(chunks), out_path)
    return out_path


# ============================================================
# Public entry
# ============================================================

def generate_captions(
    audio_path: Path,
    video_id: int,
    *,
    video_width: int = 1080,
    video_height: int = 1920,
    style: Optional[dict] = None,
) -> Optional[Path]:
    """Transcribe audio and build an ASS subtitle file. Returns path or None."""
    style = style or {}
    words = transcribe_words(audio_path,
                             language=style.get("language", "en"),
                             model_size=style.get("whisper_model", "tiny.en"))
    if not words:
        return None
    out = CAPTION_DIR / f"{video_id}_captions.ass"
    return build_ass(
        words,
        out,
        video_width=video_width,
        video_height=video_height,
        font_name=style.get("font_name", "Impact"),
        font_size=style.get("font_size", 96),
        primary_hex=style.get("primary_color", "#FFFFFF"),
        outline_hex=style.get("outline_color", "#000000"),
        highlight_hex=style.get("highlight_color", "#FFEB3B"),
        outline_px=style.get("outline_px", 6),
        margin_v=style.get("margin_v", 520),
        pop_animation=style.get("pop_animation", True),
        uppercase=style.get("uppercase", True),
        max_words_per_chunk=style.get("max_words_per_chunk", 2),
        max_chars_per_chunk=style.get("max_chars_per_chunk", 14),
    )
