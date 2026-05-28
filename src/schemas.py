"""Pydantic models for all pipeline data structures."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Stage 1 — Topic
# ---------------------------------------------------------------------------

class TopicOutput(BaseModel):
    topic: str = Field(..., min_length=3, max_length=200)
    category: str = Field(..., description="One of: history, science, space, biology, human_behavior")
    hook_angle: str = Field(..., min_length=10, max_length=300)
    target_keywords: Annotated[list[str], Field(min_length=3, max_length=3)]

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        allowed = {"history", "science", "space", "biology", "human_behavior"}
        if v.lower() not in allowed:
            raise ValueError(f"category must be one of {allowed}, got '{v}'")
        return v.lower()

    @field_validator("target_keywords")
    @classmethod
    def validate_keywords(cls, v: list[str]) -> list[str]:
        if len(v) != 3:
            raise ValueError(f"target_keywords must have exactly 3 items, got {len(v)}")
        return [kw.strip().lower() for kw in v]


# ---------------------------------------------------------------------------
# Stage 2 — Script
# ---------------------------------------------------------------------------

class SceneScript(BaseModel):
    line: str = Field(..., min_length=5, max_length=120)
    visual_prompt: str = Field(..., min_length=10, max_length=400)

    @field_validator("line")
    @classmethod
    def validate_line_word_count(cls, v: str) -> str:
        words = v.split()
        if len(words) > 15:
            raise ValueError(f"scene line must be ≤15 words, got {len(words)}: '{v}'")
        return v


class ScriptOutput(BaseModel):
    hook: str = Field(..., min_length=5, max_length=100)
    scenes: Annotated[list[SceneScript], Field(min_length=6, max_length=8)]
    cta: str = Field(..., min_length=10, max_length=200)
    uncertain_claims: bool = Field(
        default=False,
        description="True if model flagged uncertain claims; triggers topic re-roll",
    )

    @field_validator("hook")
    @classmethod
    def validate_hook_word_count(cls, v: str) -> str:
        words = v.split()
        if len(words) > 12:
            raise ValueError(f"hook must be ≤12 words, got {len(words)}: '{v}'")
        return v

    @field_validator("cta")
    @classmethod
    def validate_cta_not_generic(cls, v: str) -> str:
        banned = {"what do you think", "let me know", "share your thoughts"}
        low = v.lower()
        for phrase in banned:
            if phrase in low:
                raise ValueError(f"CTA is too generic, contains '{phrase}'")
        return v

    @model_validator(mode="after")
    def validate_scene_count(self) -> "ScriptOutput":
        n = len(self.scenes)
        if not (6 <= n <= 8):
            raise ValueError(f"scenes must be 6–8, got {n}")
        return self


# ---------------------------------------------------------------------------
# Stage 5 — Captions
# ---------------------------------------------------------------------------

class WordTimestamp(BaseModel):
    word: str
    start: float = Field(..., ge=0.0)
    end: float = Field(..., ge=0.0)

    @model_validator(mode="after")
    def end_after_start(self) -> "WordTimestamp":
        if self.end <= self.start:
            raise ValueError(f"end ({self.end}) must be > start ({self.start})")
        return self


class CaptionChunk(BaseModel):
    """Max-3-word group displayed together on screen."""
    words: Annotated[list[WordTimestamp], Field(min_length=1, max_length=3)]
    start: float
    end: float

    @model_validator(mode="after")
    def sync_times(self) -> "CaptionChunk":
        if self.words:
            self.start = self.words[0].start
            self.end = self.words[-1].end
        return self


class CaptionsOutput(BaseModel):
    chunks: list[CaptionChunk]
    total_duration: float = Field(..., gt=0.0)


# ---------------------------------------------------------------------------
# Stage 7 — Metadata
# ---------------------------------------------------------------------------

class YouTubeMetadata(BaseModel):
    title: str = Field(..., min_length=10, max_length=60)
    description: str = Field(..., min_length=200, max_length=5000)
    tags: Annotated[list[str], Field(min_length=15, max_length=15)]
    category_id: int = Field(..., description="27=Education, 28=Science & Tech")

    @field_validator("title")
    @classmethod
    def validate_title_formula(cls, v: str) -> str:
        if not v.endswith("#Shorts"):
            raise ValueError("YouTube title must end with '#Shorts'")
        return v

    @field_validator("category_id")
    @classmethod
    def validate_category_id(cls, v: int) -> int:
        if v not in {27, 28}:
            raise ValueError(f"category_id must be 27 or 28, got {v}")
        return v

    @field_validator("tags")
    @classmethod
    def validate_tag_count(cls, v: list[str]) -> list[str]:
        if len(v) != 15:
            raise ValueError(f"YouTube tags must be exactly 15, got {len(v)}")
        return v


class InstagramMetadata(BaseModel):
    caption: str = Field(..., min_length=20, max_length=2200)
    hashtags: Annotated[list[str], Field(min_length=25, max_length=25)]

    @field_validator("caption")
    @classmethod
    def validate_first_line_length(cls, v: str) -> str:
        first_line = v.split("\n")[0]
        if len(first_line) > 125:
            raise ValueError(
                f"Instagram caption first line must be ≤125 chars for preview, got {len(first_line)}"
            )
        return v

    @field_validator("hashtags")
    @classmethod
    def validate_hashtag_count(cls, v: list[str]) -> list[str]:
        if len(v) != 25:
            raise ValueError(f"Instagram hashtags must be exactly 25, got {len(v)}")
        # Ensure they all start with #
        return [h if h.startswith("#") else f"#{h}" for h in v]


class MetadataOutput(BaseModel):
    youtube: YouTubeMetadata
    instagram: InstagramMetadata
    # Hashtag block posted as a first comment (cleaner caption, same SEO benefit)
    instagram_first_comment: str = Field(default="")
    generated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Stage 8 — Upload results
# ---------------------------------------------------------------------------

class YouTubeUploadResult(BaseModel):
    video_id: str
    url: str
    privacy_status: str


class InstagramUploadResult(BaseModel):
    media_id: str
    permalink: str | None = None


class UploadResult(BaseModel):
    run_date: str  # YYYY-MM-DD
    topic: str
    youtube: YouTubeUploadResult | None = None
    instagram: InstagramUploadResult | None = None
    uploaded_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Stage 9 — Analytics
# ---------------------------------------------------------------------------

class VideoStats(BaseModel):
    run_date: str
    platform: str  # "youtube" | "instagram"
    video_id: str
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    watch_time_seconds: float | None = None
    fetched_at: datetime = Field(default_factory=datetime.utcnow)
