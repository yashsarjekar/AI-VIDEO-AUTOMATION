"""Tests for metadata character limits and the title-formula enforcer."""

import pytest
from pydantic import ValidationError

from src.schemas import InstagramMetadata, MetadataOutput, YouTubeMetadata


# ---------------------------------------------------------------------------
# Title formula enforcer
# ---------------------------------------------------------------------------

def _make_yt(**overrides) -> dict:
    base = dict(
        title="3 Animal Facts Scientists Got Completely Wrong #Shorts",
        description=("Hook restated line one. Hook restated line two.\n\n" + "Body text. " * 25),
        tags=[f"tag{i}" for i in range(15)],
        category_id=27,
    )
    return {**base, **overrides}


def test_title_formula_digit_required():
    """Title without any digit must be rejected."""
    with pytest.raises(ValidationError, match="number or specific detail"):
        YouTubeMetadata(**_make_yt(title="Amazing Animal Facts No One Knows #Shorts"))


def test_title_formula_shorts_suffix_required():
    with pytest.raises(ValidationError, match="#Shorts"):
        YouTubeMetadata(**_make_yt(title="3 Animal Facts Scientists Got Wrong"))


def test_title_formula_both_violations():
    """Title with neither digit nor #Shorts must fail."""
    with pytest.raises(ValidationError):
        YouTubeMetadata(**_make_yt(title="Amazing facts nobody knows about"))


def test_title_at_exactly_60_chars():
    """60-character title is at the boundary — must pass."""
    title = "A" * 51 + " 1 #Shorts"  # 51 + 9 = 60 chars, has digit, ends with #Shorts
    m = YouTubeMetadata(**_make_yt(title=title))
    assert len(m.title) == 60


def test_title_at_61_chars_rejected():
    title = "A" * 52 + " 1 #Shorts"  # 61 chars
    with pytest.raises(ValidationError):
        YouTubeMetadata(**_make_yt(title=title))


# ---------------------------------------------------------------------------
# Description length
# ---------------------------------------------------------------------------

def test_description_min_length():
    """Description < 200 chars must be rejected."""
    with pytest.raises(ValidationError):
        YouTubeMetadata(**_make_yt(description="Too short."))


def test_description_valid_length():
    desc = "W " * 250  # 500 chars, ~250 words
    m = YouTubeMetadata(**_make_yt(description=desc.strip()))
    assert len(m.description) > 200


# ---------------------------------------------------------------------------
# YouTube tags
# ---------------------------------------------------------------------------

def test_tags_exactly_15():
    m = YouTubeMetadata(**_make_yt())
    assert len(m.tags) == 15


def test_tags_14_rejected():
    with pytest.raises(ValidationError, match="exactly 15"):
        YouTubeMetadata(**_make_yt(tags=[f"tag{i}" for i in range(14)]))


def test_tags_16_rejected():
    with pytest.raises(ValidationError, match="exactly 15"):
        YouTubeMetadata(**_make_yt(tags=[f"tag{i}" for i in range(16)]))


# ---------------------------------------------------------------------------
# Instagram first-line limit
# ---------------------------------------------------------------------------

def _make_ig(**overrides) -> dict:
    base = dict(
        caption="Short first line\n\nExpansion.\n\nCTA?\n.\n.\n.\n#reels",
        hashtags=[f"#tag{i}" for i in range(25)],
    )
    return {**base, **overrides}


def test_ig_first_line_exactly_125():
    first_line = "A" * 125
    m = InstagramMetadata(**_make_ig(caption=f"{first_line}\n\nExpansion."))
    assert m.caption.split("\n")[0] == first_line


def test_ig_first_line_126_rejected():
    first_line = "A" * 126
    with pytest.raises(ValidationError, match="≤125 chars"):
        InstagramMetadata(**_make_ig(caption=f"{first_line}\n\nExpansion."))


# ---------------------------------------------------------------------------
# Instagram hashtags
# ---------------------------------------------------------------------------

def test_ig_hashtags_exactly_25():
    m = InstagramMetadata(**_make_ig())
    assert len(m.hashtags) == 25


def test_ig_hashtags_auto_prefixed():
    tags_no_hash = [f"tag{i}" for i in range(25)]
    m = InstagramMetadata(**_make_ig(hashtags=tags_no_hash))
    assert all(h.startswith("#") for h in m.hashtags)


# ---------------------------------------------------------------------------
# MetadataOutput first_comment field
# ---------------------------------------------------------------------------

def test_metadata_output_first_comment_default():
    yt = YouTubeMetadata(**_make_yt())
    ig = InstagramMetadata(**_make_ig())
    meta = MetadataOutput(youtube=yt, instagram=ig)
    assert meta.instagram_first_comment == ""


def test_metadata_output_first_comment_set():
    yt = YouTubeMetadata(**_make_yt())
    ig = InstagramMetadata(**_make_ig())
    meta = MetadataOutput(youtube=yt, instagram=ig, instagram_first_comment="#hash1 #hash2")
    assert meta.instagram_first_comment == "#hash1 #hash2"
