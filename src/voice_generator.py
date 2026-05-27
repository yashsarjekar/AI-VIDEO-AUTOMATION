"""Stage 4 — TTS via ElevenLabs Flash v2.5, one clip per segment + silence-padded concat."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import yaml
from elevenlabs.client import ElevenLabs
from elevenlabs import VoiceSettings
from loguru import logger
from pydub import AudioSegment

from .db import log_api_cost
from .schemas import ScriptOutput

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

def _load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)

# ---------------------------------------------------------------------------
# Segment naming convention
#
#   voice_00.mp3   → hook
#   voice_01.mp3   → scene 1
#   …
#   voice_N.mp3    → scene N
#   voice_cta.mp3  → CTA
#   voice_full.mp3 → full concatenation with 150 ms silence between each
# ---------------------------------------------------------------------------

SILENCE_MS = 150   # milliseconds of silence between segments

# ElevenLabs Flash v2.5 pricing estimate (USD per character)
_EL_COST_PER_CHAR = 0.30 / 1000

# ---------------------------------------------------------------------------
# ElevenLabs TTS
# ---------------------------------------------------------------------------

def _tts(client: ElevenLabs, text: str, voice_id: str, model: str) -> bytes:
    """Call ElevenLabs and return raw MP3 bytes."""
    audio_chunks = client.text_to_speech.convert(
        voice_id=voice_id,
        text=text,
        model_id=model,
        output_format="mp3_44100_128",
        voice_settings=VoiceSettings(
            stability=0.50,
            similarity_boost=0.75,
            style=0.0,
            use_speaker_boost=True,
        ),
    )
    return b"".join(audio_chunks)


def _voice_for_segment(
    client: ElevenLabs,
    text: str,
    out_path: Path,
    voice_id: str,
    model: str,
    label: str,
) -> int:
    """Generate TTS for one segment, save to out_path. Returns character count."""
    if out_path.exists():
        logger.info(f"  {label} already exists, skipping TTS call.")
        return 0  # no new chars billed

    logger.info(f"  TTS → {label}: '{text[:60]}{'…' if len(text) > 60 else ''}'")
    audio_bytes = _tts(client, text, voice_id, model)
    out_path.write_bytes(audio_bytes)
    logger.debug(f"  Saved {out_path.name} ({len(audio_bytes) // 1024} KB)")
    return len(text)


# ---------------------------------------------------------------------------
# Concatenation
# ---------------------------------------------------------------------------

def _concatenate(segment_paths: list[Path], out_path: Path) -> None:
    """Join MP3 files with SILENCE_MS of silence between each segment."""
    silence = AudioSegment.silent(duration=SILENCE_MS, frame_rate=44100)
    combined = AudioSegment.empty()

    for i, path in enumerate(segment_paths):
        segment = AudioSegment.from_mp3(str(path))
        combined += segment
        if i < len(segment_paths) - 1:
            combined += silence

    combined.export(str(out_path), format="mp3", bitrate="128k")
    duration_sec = len(combined) / 1000
    logger.success(
        f"voice_full.mp3 → {out_path.name} "
        f"({duration_sec:.1f}s, {out_path.stat().st_size // 1024} KB)"
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_voices(
    run_date: str | None = None,
    script: ScriptOutput | None = None,
) -> list[Path]:
    """Generate per-segment voice files and concatenate to voice_full.mp3.

    Returns ordered list of individual segment paths (hook, scenes, CTA).
    Idempotent at both segment and full-concat level.
    """
    if run_date is None:
        run_date = datetime.utcnow().strftime("%Y-%m-%d")

    run_dir = Path(__file__).parent.parent / "runs" / run_date
    full_path = run_dir / "voice_full.mp3"

    # Load script if not provided
    if script is None:
        script_path = run_dir / "script.json"
        if not script_path.exists():
            raise FileNotFoundError(
                f"script.json not found for {run_date}. Run script_writer first."
            )
        script = ScriptOutput.model_validate_json(script_path.read_text())

    run_dir.mkdir(parents=True, exist_ok=True)

    config = _load_config()
    voice_id: str = config["tts"]["voice_id"]
    model: str = config["tts"]["model"]

    if voice_id == "REPLACE_WITH_VOICE_ID":
        raise ValueError(
            "config.yaml tts.voice_id is not set. "
            "Choose a voice at elevenlabs.io and set the ID."
        )

    # Build ordered segment list: hook → scenes → CTA
    segments: list[tuple[str, Path]] = [
        (script.hook, run_dir / "voice_00.mp3"),
    ]
    for i, scene in enumerate(script.scenes, start=1):
        segments.append((scene.line, run_dir / f"voice_{i:02d}.mp3"))
    segments.append((script.cta, run_dir / "voice_cta.mp3"))

    segment_paths = [path for _, path in segments]

    # Full-concat idempotency: if voice_full.mp3 exists AND all segments exist, skip
    all_exist = all(p.exists() for p in segment_paths) and full_path.exists()
    if all_exist:
        logger.info(f"All voice files already exist for {run_date}, skipping.")
        return segment_paths

    client = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
    total_chars = 0

    logger.info(f"Generating {len(segments)} voice segments via ElevenLabs {model}")
    for text, path in segments:
        label = path.name
        chars = _voice_for_segment(client, text, path, voice_id, model, label)
        total_chars += chars

    # Cost logging
    if total_chars > 0:
        cost = total_chars * _EL_COST_PER_CHAR
        log_api_cost(run_date, "elevenlabs", cost)
        logger.info(f"ElevenLabs: {total_chars} chars, estimated cost ${cost:.5f}")

    # Concatenate all segments to voice_full.mp3
    _concatenate(segment_paths, full_path)

    return segment_paths
