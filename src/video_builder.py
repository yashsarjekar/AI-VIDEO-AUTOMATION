"""Stage 6 — Video assembly: zoompan Ken Burns + ASS captions + music mix via ffmpeg-python."""

from __future__ import annotations

import random
from datetime import datetime
from pathlib import Path

import ffmpeg
import yaml
from loguru import logger
from pydub import AudioSegment as PA

from .schemas import CaptionsOutput, ScriptOutput

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FPS = 25
W, H = 1080, 1920
FONT_NAME = "Inter Bold"
FONT_SIZE = 72

# ASS color format is &HAABBGGRR (alpha, blue, green, red)
# Yellow #FFD700: R=FF, G=D7, B=00 → &H0000D7FF
# White  #FFFFFF: R=FF, G=FF, B=FF → &H00FFFFFF
YELLOW_ASS = "&H0000D7FF"
WHITE_ASS = "&H00FFFFFF"
BLACK_ASS = "&H00000000"
SHADOW_ASS = "&H80000000"  # 50% transparent black

SILENCE_GAP_SEC = 0.150  # must match voice_generator.SILENCE_MS / 1000

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

def _load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# ASS subtitle file
# ---------------------------------------------------------------------------

def _ass_timecode(seconds: float) -> str:
    """Convert seconds to ASS timecode H:MM:SS.cc"""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds % 1) * 100))
    if cs == 100:
        s += 1
        cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape_ass(text: str) -> str:
    """Escape characters that would break ASS dialogue lines."""
    return text.replace("\\", "\\\\ ").replace("{", "\\{").replace("}", "\\}").replace("\n", " ")


def _build_ass(captions: CaptionsOutput, ass_path: Path) -> None:
    """Write ASS file: one Dialogue line per word, full chunk shown, active word in yellow."""
    header = "\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {W}",
        f"PlayResY: {H}",
        "ScaledBorderAndShadow: yes",
        "WrapStyle: 0",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        # Alignment=2 → bottom-center; MarginV=440 → 440 px from bottom ≈ y=1400 from top
        f"Style: Default,{FONT_NAME},{FONT_SIZE},"
        f"{WHITE_ASS},{YELLOW_ASS},{BLACK_ASS},{SHADOW_ASS},"
        "1,0,0,0,100,100,2,0,1,3,2,2,40,40,440,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ])

    dialogue_lines: list[str] = []
    for chunk in captions.chunks:
        words = chunk.words
        n = len(words)
        for wi, active_word in enumerate(words):
            # Each word is "active" from its own start until the next word starts
            t_start = active_word.start
            t_end = words[wi + 1].start if wi < n - 1 else chunk.end
            if t_end <= t_start:
                t_end = t_start + 0.04

            # Build chunk text: active word in yellow, others in default (white)
            parts: list[str] = []
            for j, w in enumerate(words):
                clean = _escape_ass(w.word)
                if j == wi:
                    parts.append(f"{{\\1c{YELLOW_ASS}&}}{clean}{{\\r}}")
                else:
                    parts.append(clean)

            text = " ".join(parts)
            dialogue_lines.append(
                f"Dialogue: 0,{_ass_timecode(t_start)},{_ass_timecode(t_end)},"
                f"Default,,0,0,0,,{text}"
            )

    ass_path.write_text(header + "\n" + "\n".join(dialogue_lines), encoding="utf-8")
    logger.info(f"ASS file written: {ass_path.name} ({len(dialogue_lines)} dialogue lines)")


# ---------------------------------------------------------------------------
# Clip schedule
# ---------------------------------------------------------------------------

def _get_clip_schedule(run_dir: Path, script: ScriptOutput) -> list[tuple[Path, float]]:
    """Return (image_path, clip_duration_sec) for each voice segment.

    Segment order: voice_00 (hook), voice_01…N (scenes), voice_cta (CTA).
    Each duration includes the 150 ms gap, except the last segment.
    """
    num_scenes = len(script.scenes)

    # voice file → image file mapping
    segments: list[tuple[Path, Path]] = [
        (run_dir / "voice_00.mp3", run_dir / "scene_01.jpg"),  # hook → first scene image
    ]
    for i in range(1, num_scenes + 1):
        segments.append((run_dir / f"voice_{i:02d}.mp3", run_dir / f"scene_{i:02d}.jpg"))
    segments.append(
        (run_dir / "voice_cta.mp3", run_dir / f"scene_{num_scenes:02d}.jpg")  # CTA → last image
    )

    schedule: list[tuple[Path, float]] = []
    for idx, (vf, img) in enumerate(segments):
        if not vf.exists():
            raise FileNotFoundError(f"Voice segment not found: {vf.name}")
        if not img.exists():
            raise FileNotFoundError(f"Scene image not found: {img.name}")
        duration_sec = len(PA.from_mp3(str(vf))) / 1000.0
        if idx < len(segments) - 1:
            duration_sec += SILENCE_GAP_SEC
        schedule.append((img, round(duration_sec, 3)))

    return schedule


# ---------------------------------------------------------------------------
# Music
# ---------------------------------------------------------------------------

def _pick_music(seed: int) -> Path | None:
    music_dir = Path(__file__).parent.parent / "assets" / "music"
    tracks = sorted(music_dir.glob("*.mp3")) + sorted(music_dir.glob("*.wav"))
    if not tracks:
        logger.warning("No music files in assets/music/ — output will have no background music.")
        return None
    track = random.Random(seed).choice(tracks)
    logger.info(f"Music: {track.name}")
    return track


# ---------------------------------------------------------------------------
# ffmpeg pipeline
# ---------------------------------------------------------------------------

def _build_ffmpeg_video(
    clip_schedule: list[tuple[Path, float]],
    voice_full_path: Path,
    music_path: Path | None,
    ass_path: Path,
    output_path: Path,
) -> None:
    fonts_dir = Path(__file__).parent.parent / "assets" / "fonts"

    # --- Per-scene video streams ---
    video_streams = []
    for i, (image_path, duration) in enumerate(clip_schedule):
        total_frames = max(2, int(round(duration * FPS)))

        if i % 2 == 0:
            # Zoom-in: 1.0 → 1.12 over the clip
            z_expr = f"min(1+0.12*on/{total_frames},1.12)"
            x_expr = "iw/2-(iw/zoom/2)"
            y_expr = "ih/2-(ih/zoom/2)"
        else:
            # Pan-right: fixed 1.08× zoom, drift 4% of frame width
            z_expr = "1.08"
            x_expr = f"iw/2-(iw/zoom/2)+(on/{total_frames})*iw*0.04"
            y_expr = "ih/2-(ih/zoom/2)"

        stream = (
            ffmpeg
            .input(str(image_path), loop=1, t=duration)
            .filter("scale", W, H,
                    force_original_aspect_ratio="decrease",
                    force_divisible_by=2)
            .filter("pad", W, H, "(ow-iw)/2", "(oh-ih)/2", color="black")
            .filter("zoompan",
                    z=z_expr, x=x_expr, y=y_expr,
                    d=total_frames,
                    s=f"{W}x{H}",
                    fps=FPS)
            .filter("setsar", "1/1")
            .filter("setpts", "PTS-STARTPTS")
        )
        video_streams.append(stream)

    # --- Concat ---
    concat_video = (
        video_streams[0]
        if len(video_streams) == 1
        else ffmpeg.concat(*video_streams, v=1, a=0)
    )

    # --- Caption overlay ---
    with_subs = concat_video.filter(
        "subtitles",
        filename=str(ass_path),
        fontsdir=str(fonts_dir),
    )

    # --- Audio mix ---
    voice_audio = ffmpeg.input(str(voice_full_path)).audio
    total_duration = sum(d for _, d in clip_schedule)

    if music_path is not None:
        music_audio = (
            ffmpeg
            .input(str(music_path), stream_loop=-1, t=total_duration)
            .audio
            .filter("volume", volume="0.0794")  # -22 dBFS
        )
        mixed_audio = ffmpeg.filter(
            [voice_audio, music_audio],
            "amix",
            inputs=2,
            duration="first",
            dropout_transition=2,
        )
    else:
        mixed_audio = voice_audio

    # --- Encode ---
    (
        ffmpeg
        .output(
            with_subs,
            mixed_audio,
            str(output_path),
            vcodec="libx264",
            acodec="aac",
            crf=23,
            maxrate="8M",
            bufsize="16M",
            movflags="faststart",
            r=FPS,
            pix_fmt="yuv420p",
            audio_bitrate="128k",
        )
        .run(overwrite_output=True, quiet=False)
    )

    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.success(f"final.mp4 → {size_mb:.1f} MB")
    if size_mb > 80:
        logger.warning(f"{size_mb:.1f} MB exceeds 80 MB target. Consider reducing CRF or scene count.")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_video(
    run_date: str | None = None,
    script: ScriptOutput | None = None,
    captions: CaptionsOutput | None = None,
) -> Path:
    """Assemble final.mp4 for the given run date. Idempotent."""
    if run_date is None:
        run_date = datetime.utcnow().strftime("%Y-%m-%d")

    run_dir = Path(__file__).parent.parent / "runs" / run_date
    output_path = run_dir / "final.mp4"

    if output_path.exists():
        logger.info(f"final.mp4 already exists for {run_date}, skipping.")
        return output_path

    # Load script
    if script is None:
        sp = run_dir / "script.json"
        if not sp.exists():
            raise FileNotFoundError(f"script.json not found for {run_date}.")
        script = ScriptOutput.model_validate_json(sp.read_text())

    # Load captions
    if captions is None:
        cp = run_dir / "captions.json"
        if not cp.exists():
            raise FileNotFoundError(f"captions.json not found for {run_date}.")
        captions = CaptionsOutput.model_validate_json(cp.read_text())

    voice_full = run_dir / "voice_full.mp3"
    if not voice_full.exists():
        raise FileNotFoundError(f"voice_full.mp3 not found for {run_date}.")

    clip_schedule = _get_clip_schedule(run_dir, script)
    total_sec = sum(d for _, d in clip_schedule)
    logger.info(f"Clip schedule: {len(clip_schedule)} segments, {total_sec:.1f}s total")

    ass_path = run_dir / "captions.ass"
    if not ass_path.exists():
        _build_ass(captions, ass_path)

    seed = int(run_date.replace("-", ""))
    music_path = _pick_music(seed)

    run_dir.mkdir(parents=True, exist_ok=True)
    _build_ffmpeg_video(clip_schedule, voice_full, music_path, ass_path, output_path)

    return output_path
