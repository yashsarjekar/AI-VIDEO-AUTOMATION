"""Tests that each pipeline stage skips work when its output file already exists."""

import shutil
from pathlib import Path

import pytest

from src.schemas import (
    CaptionsOutput,
    MetadataOutput,
    ScriptOutput,
    TopicOutput,
    WordTimestamp,
    CaptionChunk,
    YouTubeMetadata,
    InstagramMetadata,
)

# Use a clearly synthetic date that will never collide with a real run
_TEST_DATE = "0001-01-01"
_REPO_ROOT = Path(__file__).parent.parent
_TEST_RUN_DIR = _REPO_ROOT / "runs" / _TEST_DATE


@pytest.fixture(autouse=True)
def clean_test_run_dir():
    """Create and tear down the test run directory around each test."""
    _TEST_RUN_DIR.mkdir(parents=True, exist_ok=True)
    yield
    shutil.rmtree(_TEST_RUN_DIR, ignore_errors=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sample_topic() -> TopicOutput:
    return TopicOutput(
        topic="Sharks are older than trees",
        category="science",
        hook_angle="Trees didn't exist when sharks first evolved",
        target_keywords=["shark facts", "evolution", "science facts"],
    )


def _sample_script() -> ScriptOutput:
    scenes = [
        {"line": f"Scene {i} with some words.", "visual_prompt": f"Vivid image {i}"}
        for i in range(1, 7)
    ]
    return ScriptOutput(
        hook="450 million years before trees existed.",
        scenes=scenes,
        cta="Which prehistoric fact surprised you more — sharks or trees?",
        uncertain_claims=False,
    )


def _sample_captions() -> CaptionsOutput:
    words = [
        WordTimestamp(word="Hello", start=0.0, end=0.3),
        WordTimestamp(word="world", start=0.3, end=0.6),
    ]
    chunk = CaptionChunk(words=words, start=0.0, end=0.6)
    return CaptionsOutput(chunks=[chunk], total_duration=0.6)


def _sample_metadata() -> MetadataOutput:
    yt = YouTubeMetadata(
        title="450 Million Years Before Trees — Sharks Existed 1st #Shorts",
        description=("Hook line one. Hook line two.\n\n" + "Body details. " * 20).strip(),
        tags=[f"tag{i}" for i in range(15)],
        category_id=27,
    )
    ig = InstagramMetadata(
        caption="Sharks are older than trees by 200 million years\n\nExpansion.\n\nCTA?\n.\n.\n.\n#reels",
        hashtags=[f"#tag{i}" for i in range(25)],
    )
    return MetadataOutput(youtube=yt, instagram=ig, instagram_first_comment="#extra #tags")


# ---------------------------------------------------------------------------
# topic_generator
# ---------------------------------------------------------------------------

def test_topic_generator_uses_cache():
    """generate_topic() must return the cached file without calling Claude."""
    topic = _sample_topic()
    cache_file = _TEST_RUN_DIR / "topic.json"
    cache_file.write_text(topic.model_dump_json())

    # Import after fixture setup to avoid issues with module-level paths
    from src.topic_generator import generate_topic

    # No ANTHROPIC_API_KEY set — would raise KeyError if Claude was called
    result = generate_topic(_TEST_DATE)
    assert result.topic == topic.topic
    assert result.category == topic.category


# ---------------------------------------------------------------------------
# script_writer
# ---------------------------------------------------------------------------

def test_script_writer_uses_cache():
    """generate_script() must return the cached file without calling Claude."""
    topic = _sample_topic()
    script = _sample_script()

    (_TEST_RUN_DIR / "topic.json").write_text(topic.model_dump_json())
    (_TEST_RUN_DIR / "script.json").write_text(script.model_dump_json())

    from src.script_writer import generate_script

    result_script, result_topic = generate_script(_TEST_DATE)
    assert result_script.hook == script.hook
    assert result_topic.topic == topic.topic


# ---------------------------------------------------------------------------
# caption_generator
# ---------------------------------------------------------------------------

def test_caption_generator_uses_cache():
    """generate_captions() must return the cached file without running Whisper."""
    captions = _sample_captions()
    (_TEST_RUN_DIR / "captions.json").write_text(captions.model_dump_json())

    from src.caption_generator import generate_captions

    result = generate_captions(_TEST_DATE)
    assert result.total_duration == captions.total_duration
    assert len(result.chunks) == len(captions.chunks)


# ---------------------------------------------------------------------------
# metadata_generator
# ---------------------------------------------------------------------------

def test_metadata_generator_uses_cache():
    """generate_metadata() must return the cached file without calling Claude."""
    metadata = _sample_metadata()
    (_TEST_RUN_DIR / "metadata.json").write_text(metadata.model_dump_json())

    from src.metadata_generator import generate_metadata

    result = generate_metadata(_TEST_DATE)
    assert result.youtube.title == metadata.youtube.title
    assert result.instagram_first_comment == metadata.instagram_first_comment


# ---------------------------------------------------------------------------
# video_builder
# ---------------------------------------------------------------------------

def test_video_builder_uses_cache():
    """build_video() must return the cached final.mp4 path without running ffmpeg."""
    final_mp4 = _TEST_RUN_DIR / "final.mp4"
    final_mp4.write_bytes(b"fake video content")

    from src.video_builder import build_video

    result = build_video(_TEST_DATE)
    assert result == final_mp4
    # File must not have been modified (ffmpeg would rewrite it)
    assert final_mp4.read_bytes() == b"fake video content"
