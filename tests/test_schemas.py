"""Tests for all Pydantic schema validators."""

import pytest
from pydantic import ValidationError

from src.schemas import (
    CaptionChunk,
    InstagramMetadata,
    SceneScript,
    ScriptOutput,
    TopicOutput,
    WordTimestamp,
    YouTubeMetadata,
)

# ---------------------------------------------------------------------------
# TopicOutput
# ---------------------------------------------------------------------------

VALID_TOPIC = dict(
    topic="Octopuses have three hearts and blue blood",
    category="biology",
    hook_angle="Two of their hearts stop beating when they swim",
    target_keywords=["octopus facts", "marine biology", "biology facts"],
)


def test_topic_valid():
    t = TopicOutput(**VALID_TOPIC)
    assert t.category == "biology"
    assert len(t.target_keywords) == 3


def test_topic_invalid_category():
    with pytest.raises(ValidationError, match="category"):
        TopicOutput(**{**VALID_TOPIC, "category": "cooking"})


def test_topic_keywords_too_few():
    with pytest.raises(ValidationError):
        TopicOutput(**{**VALID_TOPIC, "target_keywords": ["only", "two"]})


def test_topic_keywords_too_many():
    with pytest.raises(ValidationError):
        TopicOutput(**{**VALID_TOPIC, "target_keywords": ["a", "b", "c", "d"]})


def test_topic_keywords_normalised():
    t = TopicOutput(**{**VALID_TOPIC, "target_keywords": [" Space Facts ", "HISTORY", "science"]})
    assert t.target_keywords == ["space facts", "history", "science"]


# ---------------------------------------------------------------------------
# SceneScript
# ---------------------------------------------------------------------------

def test_scene_line_too_long():
    too_long = "word " * 16  # 16 words
    with pytest.raises(ValidationError, match="≤15 words"):
        SceneScript(line=too_long.strip(), visual_prompt="A glowing nebula")


def test_scene_line_exactly_15_words():
    line = " ".join(["word"] * 15)
    s = SceneScript(line=line, visual_prompt="Vivid image of something specific")
    assert len(s.line.split()) == 15


# ---------------------------------------------------------------------------
# ScriptOutput
# ---------------------------------------------------------------------------

def _make_scenes(n: int) -> list[dict]:
    return [
        {"line": f"Scene {i} has some words here.", "visual_prompt": f"Visual prompt {i} image"}
        for i in range(1, n + 1)
    ]


VALID_SCRIPT = dict(
    hook="5 seconds changed everything we thought we knew",
    scenes=_make_scenes(7),
    cta="Would you rather have three hearts or eight arms?",
    uncertain_claims=False,
)


def test_script_valid():
    s = ScriptOutput(**VALID_SCRIPT)
    assert len(s.scenes) == 7
    assert s.uncertain_claims is False


def test_script_hook_too_long():
    long_hook = "word " * 13  # 13 words
    with pytest.raises(ValidationError, match="≤12 words"):
        ScriptOutput(**{**VALID_SCRIPT, "hook": long_hook.strip()})


def test_script_too_few_scenes():
    with pytest.raises(ValidationError):
        ScriptOutput(**{**VALID_SCRIPT, "scenes": _make_scenes(5)})


def test_script_too_many_scenes():
    with pytest.raises(ValidationError):
        ScriptOutput(**{**VALID_SCRIPT, "scenes": _make_scenes(9)})


def test_script_generic_cta_rejected():
    with pytest.raises(ValidationError, match="too generic"):
        ScriptOutput(**{**VALID_SCRIPT, "cta": "What do you think about this?"})


def test_script_six_scenes_accepted():
    s = ScriptOutput(**{**VALID_SCRIPT, "scenes": _make_scenes(6)})
    assert len(s.scenes) == 6


def test_script_eight_scenes_accepted():
    s = ScriptOutput(**{**VALID_SCRIPT, "scenes": _make_scenes(8)})
    assert len(s.scenes) == 8


# ---------------------------------------------------------------------------
# WordTimestamp / CaptionChunk
# ---------------------------------------------------------------------------

def test_word_timestamp_end_after_start():
    with pytest.raises(ValidationError, match="end.*>.*start"):
        WordTimestamp(word="hello", start=1.5, end=1.0)


def test_word_timestamp_equal_times_rejected():
    with pytest.raises(ValidationError):
        WordTimestamp(word="hello", start=1.0, end=1.0)


def test_caption_chunk_times_synced():
    words = [
        WordTimestamp(word="The", start=0.5, end=0.7),
        WordTimestamp(word="Amazon", start=0.7, end=1.1),
        WordTimestamp(word="River", start=1.1, end=1.5),
    ]
    chunk = CaptionChunk(words=words, start=0.0, end=0.0)
    assert chunk.start == 0.5
    assert chunk.end == 1.5


# ---------------------------------------------------------------------------
# YouTubeMetadata
# ---------------------------------------------------------------------------

VALID_YT = dict(
    title="42 Seconds Almost Wiped Out Dinosaurs Forever #Shorts",
    description=("Hook line one. Hook line two.\n\n" + "Detail paragraph. " * 20).strip(),
    tags=[f"tag{i}" for i in range(15)],
    category_id=27,
)


def test_yt_metadata_valid():
    m = YouTubeMetadata(**VALID_YT)
    assert m.category_id == 27


def test_yt_title_no_shorts_suffix():
    bad = VALID_YT["title"].replace(" #Shorts", "")
    with pytest.raises(ValidationError, match="#Shorts"):
        YouTubeMetadata(**{**VALID_YT, "title": bad})


def test_yt_title_no_digit():
    with pytest.raises(ValidationError, match="number or specific detail"):
        YouTubeMetadata(**{**VALID_YT, "title": "Amazing fact you never knew #Shorts"})


def test_yt_title_too_long():
    long_title = "A" * 54 + " #Shorts"  # 54 + 8 = 62 chars > 60
    with pytest.raises(ValidationError):
        YouTubeMetadata(**{**VALID_YT, "title": long_title})


def test_yt_wrong_tag_count():
    with pytest.raises(ValidationError, match="exactly 15"):
        YouTubeMetadata(**{**VALID_YT, "tags": ["tag1", "tag2"]})


def test_yt_invalid_category():
    with pytest.raises(ValidationError, match="27 or 28"):
        YouTubeMetadata(**{**VALID_YT, "category_id": 22})


# ---------------------------------------------------------------------------
# InstagramMetadata
# ---------------------------------------------------------------------------

_LONG_FIRST_LINE = "A" * 126  # 126 chars > 125 limit
_VALID_FIRST_LINE = "3 facts about octopuses that will blow your mind completely"

VALID_IG = dict(
    caption=f"{_VALID_FIRST_LINE}\n\nExpansion text here.\n\nCTA question?\n.\n.\n.\n#reels",
    hashtags=[f"#tag{i}" for i in range(25)],
)


def test_ig_metadata_valid():
    m = InstagramMetadata(**VALID_IG)
    assert len(m.hashtags) == 25


def test_ig_first_line_too_long():
    with pytest.raises(ValidationError, match="≤125 chars"):
        InstagramMetadata(**{**VALID_IG, "caption": f"{_LONG_FIRST_LINE}\n\nRest."})


def test_ig_wrong_hashtag_count():
    with pytest.raises(ValidationError, match="exactly 25"):
        InstagramMetadata(**{**VALID_IG, "hashtags": ["#tag1", "#tag2"]})


def test_ig_hashtag_prepends_hash():
    m = InstagramMetadata(**{**VALID_IG, "hashtags": [f"tag{i}" for i in range(25)]})
    assert all(h.startswith("#") for h in m.hashtags)
