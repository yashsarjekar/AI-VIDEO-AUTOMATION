"""Stage 5 — Word-level captions via faster-whisper (small.en, int8)."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from loguru import logger

from .schemas import CaptionChunk, CaptionsOutput, WordTimestamp

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WHISPER_MODEL = "small.en"
WHISPER_DEVICE = "cpu"
WHISPER_COMPUTE = "int8"
MAX_WORDS_PER_CHUNK = 3

# ---------------------------------------------------------------------------
# Whisper transcription
# ---------------------------------------------------------------------------

def _transcribe(audio_path: Path) -> list[WordTimestamp]:
    """Run faster-whisper and return a flat list of word timestamps."""
    from faster_whisper import WhisperModel  # lazy import — heavy load

    logger.info(f"Loading faster-whisper model '{WHISPER_MODEL}' ({WHISPER_COMPUTE})")
    model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)

    logger.info(f"Transcribing {audio_path.name} …")
    segments, info = model.transcribe(
        str(audio_path),
        language="en",
        word_timestamps=True,
        beam_size=5,
        vad_filter=True,          # strip long silences (the 150 ms gaps between scenes)
        vad_parameters={"min_silence_duration_ms": 100},
    )

    words: list[WordTimestamp] = []
    for segment in segments:
        if segment.words is None:
            continue
        for w in segment.words:
            text = _clean_word(w.word)
            if not text:
                continue
            # Guard against rare None timestamps from VAD-trimmed edges
            if w.start is None or w.end is None:
                continue
            end = w.end if w.end > w.start else w.start + 0.05
            words.append(WordTimestamp(word=text, start=round(w.start, 3), end=round(end, 3)))

    logger.info(
        f"Transcription complete: {len(words)} words, "
        f"{info.duration:.1f}s audio, language={info.language}"
    )
    return words


def _clean_word(raw: str) -> str:
    """Strip leading/trailing whitespace; keep internal punctuation."""
    return raw.strip()


# ---------------------------------------------------------------------------
# Chunk grouping
# ---------------------------------------------------------------------------

def _group_into_chunks(words: list[WordTimestamp]) -> list[CaptionChunk]:
    """Group flat word list into MAX_WORDS_PER_CHUNK-word display chunks."""
    chunks: list[CaptionChunk] = []
    i = 0
    while i < len(words):
        group = words[i : i + MAX_WORDS_PER_CHUNK]
        chunk = CaptionChunk(
            words=group,
            start=group[0].start,
            end=group[-1].end,
        )
        chunks.append(chunk)
        i += MAX_WORDS_PER_CHUNK
    return chunks


# ---------------------------------------------------------------------------
# Scene boundary index
#
# The video builder needs to know which chunks belong to which scene clip so
# captions stay in sync after the video is cut into per-scene clips.
# We expose a helper that maps each chunk to a scene slot using voice segment
# durations from the individual voice files.
# ---------------------------------------------------------------------------

def build_scene_boundary_times(run_dir: Path, num_scenes: int) -> list[tuple[float, float]]:
    """Return [(start_sec, end_sec)] for each voice segment in order.

    Segments: voice_00 (hook) + voice_01…voice_N (scenes) + voice_cta.
    Boundaries are computed from individual MP3 durations + 150 ms gaps.
    Returns one entry per segment in the same order as generate_voices().
    """
    from pydub import AudioSegment  # noqa: PLC0415

    SILENCE_SEC = 0.150
    segment_files: list[Path] = [run_dir / "voice_00.mp3"]
    for i in range(1, num_scenes + 1):
        segment_files.append(run_dir / f"voice_{i:02d}.mp3")
    segment_files.append(run_dir / "voice_cta.mp3")

    boundaries: list[tuple[float, float]] = []
    cursor = 0.0
    for path in segment_files:
        if not path.exists():
            logger.warning(f"Voice segment not found: {path.name} — skipping boundary")
            continue
        duration = len(AudioSegment.from_mp3(str(path))) / 1000.0
        boundaries.append((round(cursor, 3), round(cursor + duration, 3)))
        cursor += duration + SILENCE_SEC

    return boundaries


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_captions(
    run_date: str | None = None,
    num_scenes: int | None = None,
) -> CaptionsOutput:
    """Transcribe voice_full.mp3 and save word-level captions.

    Idempotent: returns cached captions.json if it already exists.
    """
    if run_date is None:
        run_date = datetime.utcnow().strftime("%Y-%m-%d")

    run_dir = Path(__file__).parent.parent / "runs" / run_date
    captions_path = run_dir / "captions.json"

    # Idempotency
    if captions_path.exists():
        logger.info(f"Captions already generated for {run_date}, loading from cache.")
        return CaptionsOutput.model_validate_json(captions_path.read_text())

    audio_path = run_dir / "voice_full.mp3"
    if not audio_path.exists():
        raise FileNotFoundError(
            f"voice_full.mp3 not found for {run_date}. Run voice_generator first."
        )

    words = _transcribe(audio_path)
    if not words:
        raise RuntimeError("Whisper returned no words — check voice_full.mp3 is not silent.")

    chunks = _group_into_chunks(words)
    total_duration = words[-1].end

    captions = CaptionsOutput(chunks=chunks, total_duration=total_duration)

    run_dir.mkdir(parents=True, exist_ok=True)
    captions_path.write_text(captions.model_dump_json(indent=2))
    logger.success(
        f"Captions saved: {len(chunks)} chunks, {len(words)} words, "
        f"{total_duration:.1f}s total"
    )
    return captions
