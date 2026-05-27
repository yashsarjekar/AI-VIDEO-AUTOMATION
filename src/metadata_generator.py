"""Stage 7 — Platform metadata generation via Claude Haiku."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import anthropic
import yaml
from loguru import logger
from pydantic import ValidationError

from .db import (
    get_hashtag_usage_last_n_days,
    init_db,
    log_api_cost,
    record_hashtag_usage,
)
from .schemas import (
    InstagramMetadata,
    MetadataOutput,
    ScriptOutput,
    TopicOutput,
    YouTubeMetadata,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

def _load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Hashtag rotation helpers
# ---------------------------------------------------------------------------

_MEDIUM_HASHTAGS = [
    "#sciencefacts", "#historyfacts", "#spacefacts", "#biologyfacts",
    "#psychologyfacts", "#funfacts", "#amazingfacts", "#interestingfacts",
    "#mindblown", "#learnontiktok", "#edutok", "#educationalcontent",
    "#factsoftheday", "#knowledgeispower", "#curiosity", "#themoreyouknow",
    "#fascination", "#todayilearned", "#til", "#coolstuff",
]

def _get_rotation_excludes(platform: str) -> str:
    """Return a string listing hashtags used too frequently this week."""
    usage = get_hashtag_usage_last_n_days(platform, days=7)
    # Exclude any medium hashtag used 3+ times in 7 days
    overused = [h for h in _MEDIUM_HASHTAGS if usage.get(h, 0) >= 3]
    if not overused:
        return "(none — all medium hashtags available)"
    return ", ".join(overused)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a viral social-media growth specialist who writes metadata for YouTube Shorts and \
Instagram Reels in the facts/trivia niche.

Your metadata must make people STOP scrolling, CLICK, and COMMENT. Every field has a formula — \
follow it exactly.

=== YOUTUBE TITLE RULES (non-negotiable) ===
- ≤60 characters total (count carefully).
- Formula: [Curiosity Gap] + [Specific Detail] + [Payoff]
- MUST include at least one specific number or statistic in the first 40 characters.
- MUST end with " #Shorts" (space + #Shorts).
- NEVER start with "Did you know", "The truth about", "This is why", or any generic opener.
- Example pattern: "9 Seconds Changed Human Evolution Forever #Shorts"

=== YOUTUBE DESCRIPTION RULES ===
- 200–300 words total.
- First two lines restate the hook (shown in search previews — make them count).
- Three paragraphs expanding the fact with context, mechanism, and implications.
- One clear CTA asking a specific question.
- End with a hashtag block (8–10 hashtags on separate lines).
- NEVER use markdown headers or bullet lists.

=== YOUTUBE TAGS RULES ===
- Exactly 15 tags.
- Mix: 4 broad ("science facts", "did you know", "facts", "educational"),
  7 medium-specificity (niche of today's topic), 4 highly specific to today's topic.
- No hashtag symbols — plain text only.

=== INSTAGRAM CAPTION RULES ===
- Line 1 (the hook): ≤125 characters — this is ALL users see before tapping "more".
- Lines 2–4: 2–3 sentences expanding the fact. Punchy.
- Line 5: the CTA — a specific, answerable question.
- Lines 6–8: three lines each containing only a single period (.) — pushes hashtags below fold.
- Lines 9+: the 5 high-volume hashtags only. Put the rest in instagram_first_comment.

=== INSTAGRAM HASHTAGS RULES ===
- Exactly 25 total (including # symbol).
- Split: 5 high-volume in the caption, 20 in instagram_first_comment.
- Distribution: 5 high-volume (#reels, #facts, #didyouknow, #shorts, #viral),
  12 medium-volume, 8 niche-specific to today's topic.
- All lowercase, no spaces within a hashtag.

=== TITLE FORMULA ENFORCER ===
Before finalizing, check: does the YouTube title contain a digit? Does it create a curiosity gap? \
Does it end with " #Shorts"? If any check fails, rewrite it.

You MUST respond with ONLY valid JSON — no prose, no markdown fences:
{
  "youtube": {
    "title": "string, ≤60 chars, has a digit, ends with #Shorts",
    "description": "string, 200-300 words, structured as described",
    "tags": ["string", ...],
    "category_id": 27
  },
  "instagram": {
    "caption": "string, hook line ≤125 chars, structured as described",
    "hashtags": ["#string", ...]
  },
  "instagram_first_comment": "string, hashtag block for first comment"
}
"""

def _build_user_prompt(
    topic: TopicOutput,
    script: ScriptOutput,
    run_date: str,
    yt_exclude: str,
    ig_exclude: str,
) -> str:
    return f"""Generate platform metadata for this video:

TOPIC: {topic.topic}
CATEGORY: {topic.category}
HOOK ANGLE: {topic.hook_angle}
PRIMARY KEYWORDS: {", ".join(topic.target_keywords)}
SCRIPT HOOK: {script.hook}
SCRIPT CTA: {script.cta}
DATE: {run_date}

HASHTAG ROTATION — avoid these medium-volume tags (over-used this week):
YouTube tags to avoid: {yt_exclude}
Instagram hashtags to avoid: {ig_exclude}

Respond with ONLY the JSON object. Double-check character counts before submitting."""


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------

_HAIKU_INPUT_COST_PER_M = 0.25
_HAIKU_OUTPUT_COST_PER_M = 1.25

def _estimate_cost(usage: anthropic.types.Usage) -> float:
    return (
        usage.input_tokens * _HAIKU_INPUT_COST_PER_M / 1_000_000
        + usage.output_tokens * _HAIKU_OUTPUT_COST_PER_M / 1_000_000
    )


# ---------------------------------------------------------------------------
# Parsing + validation
# ---------------------------------------------------------------------------

def _parse_metadata(raw: dict, run_date: str) -> MetadataOutput:
    """Validate the LLM JSON output against all schema rules."""
    yt_raw = raw.get("youtube", {})
    ig_raw = raw.get("instagram", {})

    yt = YouTubeMetadata(
        title=yt_raw.get("title", ""),
        description=yt_raw.get("description", ""),
        tags=yt_raw.get("tags", []),
        category_id=yt_raw.get("category_id", 27),
    )
    ig = InstagramMetadata(
        caption=ig_raw.get("caption", ""),
        hashtags=ig_raw.get("hashtags", []),
    )
    first_comment = raw.get("instagram_first_comment", "")

    # Description word count check
    word_count = len(yt.description.split())
    if not (200 <= word_count <= 350):  # 350 gives slight tolerance over 300
        raise ValueError(
            f"YouTube description word count {word_count} outside 200–300 target"
        )

    return MetadataOutput(
        youtube=yt,
        instagram=ig,
        instagram_first_comment=first_comment,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_metadata(
    run_date: str | None = None,
    topic: TopicOutput | None = None,
    script: ScriptOutput | None = None,
) -> MetadataOutput:
    """Generate and persist platform metadata. Idempotent."""
    if run_date is None:
        run_date = datetime.utcnow().strftime("%Y-%m-%d")

    run_dir = Path(__file__).parent.parent / "runs" / run_date
    meta_path = run_dir / "metadata.json"

    # Idempotency
    if meta_path.exists():
        logger.info(f"Metadata already generated for {run_date}, loading from cache.")
        return MetadataOutput.model_validate_json(meta_path.read_text())

    # Load dependencies
    if topic is None:
        tp = run_dir / "topic.json"
        if not tp.exists():
            raise FileNotFoundError(f"topic.json not found for {run_date}.")
        topic = TopicOutput.model_validate_json(tp.read_text())

    if script is None:
        sp = run_dir / "script.json"
        if not sp.exists():
            raise FileNotFoundError(f"script.json not found for {run_date}.")
        script = ScriptOutput.model_validate_json(sp.read_text())

    init_db()
    config = _load_config()
    model: str = config["llm"]["model"]
    max_retries: int = config["llm"]["max_retries"]

    yt_exclude = _get_rotation_excludes("youtube")
    ig_exclude = _get_rotation_excludes("instagram")

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        logger.info(f"Metadata generation attempt {attempt}/{max_retries}")
        try:
            response = client.messages.create(
                model=model,
                max_tokens=2048,
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {
                        "role": "user",
                        "content": _build_user_prompt(
                            topic, script, run_date, yt_exclude, ig_exclude
                        ),
                    }
                ],
            )

            cost = _estimate_cost(response.usage)
            log_api_cost(run_date, "anthropic", cost)
            logger.info(
                f"Haiku usage — in:{response.usage.input_tokens} "
                f"out:{response.usage.output_tokens} cost:${cost:.5f}"
            )

            raw_text = response.content[0].text.strip()
            raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
            raw_text = re.sub(r"\s*```$", "", raw_text)

            raw = json.loads(raw_text)

            # Haiku often returns fewer tags/hashtags than required — pad with
            # generic fallbacks rather than burning retries on a count error.
            _YT_FALLBACKS = [
                "facts", "didyouknow", "funfacts", "trivia", "shorts",
                "learnontiktok", "educational", "science", "nature", "viral",
                "amazing", "interesting", "knowledge", "mindblowing", "trending",
            ]
            _IG_FALLBACKS = [
                "#facts", "#didyouknow", "#funfacts", "#trivia", "#shorts",
                "#reels", "#learnontiktok", "#educational", "#science", "#nature",
                "#viral", "#amazing", "#interesting", "#knowledge", "#mindblowing",
                "#trending", "#instagram", "#explore", "#factsdaily", "#factsoflife",
                "#dailyfacts", "#amazingfacts", "#sciencefacts", "#naturefacts", "#learn",
            ]
            yt_tags = raw.get("youtube", {}).get("tags", [])
            ig_tags = raw.get("instagram", {}).get("hashtags", [])
            if isinstance(yt_tags, list) and len(yt_tags) < 15:
                needed = [t for t in _YT_FALLBACKS if t not in yt_tags]
                raw["youtube"]["tags"] = (yt_tags + needed)[:15]
            if isinstance(ig_tags, list) and len(ig_tags) < 25:
                needed = [t for t in _IG_FALLBACKS if t not in ig_tags]
                raw["instagram"]["hashtags"] = (ig_tags + needed)[:25]

            metadata = _parse_metadata(raw, run_date)

            logger.success(
                f"Metadata generated: title='{metadata.youtube.title}' "
                f"({len(metadata.youtube.title)} chars)"
            )

            # Persist
            run_dir.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(metadata.model_dump_json(indent=2))

            # Record hashtag usage for rotation
            record_hashtag_usage(metadata.youtube.tags, "youtube", run_date)
            record_hashtag_usage(metadata.instagram.hashtags, "instagram", run_date)

            return metadata

        except (ValidationError, json.JSONDecodeError, ValueError, KeyError) as exc:
            last_exc = exc
            logger.warning(f"Attempt {attempt} failed validation: {exc}")
        except anthropic.APIError as exc:
            last_exc = exc
            logger.warning(f"Attempt {attempt} API error: {exc}")

    raise RuntimeError(
        f"Metadata generation failed after {max_retries} attempts. Last error: {last_exc}"
    )
