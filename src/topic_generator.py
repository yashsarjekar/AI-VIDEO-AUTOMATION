"""Stage 1 — Topic generation via Claude Haiku with Google Trends inspiration."""

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

from .db import get_recent_topics, init_db, log_api_cost, save_topic
from .schemas import TopicOutput

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

def _load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Google Trends (optional inspiration — failures are silently swallowed)
# ---------------------------------------------------------------------------

_TREND_QUERIES = ["science facts", "history facts", "did you know"]

def _fetch_trends() -> list[str]:
    try:
        from pytrends.request import TrendReq  # noqa: PLC0415

        pytrends = TrendReq(hl="en-US", tz=0, timeout=(5, 10))
        pytrends.build_payload(_TREND_QUERIES, timeframe="now 1-d", geo="US")
        related = pytrends.related_queries()

        rising: list[str] = []
        for query in _TREND_QUERIES:
            df = related.get(query, {}).get("rising")
            if df is not None and not df.empty:
                rising.extend(df["query"].head(3).tolist())

        logger.info(f"Google Trends rising queries: {rising[:9]}")
        return rising[:9]
    except Exception as exc:
        logger.warning(f"Google Trends fetch failed (non-fatal): {exc}")
        return []


# ---------------------------------------------------------------------------
# Claude prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a viral short-form video topic researcher specializing in fascinating facts and trivia.
Your job is to identify ONE topic that will perform exceptionally well as a 45-second YouTube Short / Instagram Reel.

Niche: facts and trivia spanning history, science, space, biology, and human behavior.
Audience: curious adults 18–35, English-speaking, mobile-first.
Tone: conversational, slightly playful, never condescending — like a smart friend sharing something surprising.

Strong topics have these qualities:
- Counterintuitive: "the thing you thought you knew is wrong"
- Specific: "the Amazon River actually flows INTO the ocean underground" beats "rivers are interesting"
- Visually rich: easy to find or generate striking images for each scene
- Emotionally resonant: produces genuine surprise, delight, or mild outrage

Avoid:
- Overused "Did you know water is wet" level facts
- Anything requiring controversial political, religious, or medical claims
- Topics with hard-to-verify or frequently disputed facts
- Real people's depictions or likenesses

You MUST respond with ONLY valid JSON matching this exact schema — no prose, no markdown fences:
{
  "topic": "string, 3-200 chars, the specific fascinating fact or phenomenon",
  "category": "one of: history | science | space | biology | human_behavior",
  "hook_angle": "string, 10-300 chars, the counterintuitive or surprising angle that makes this shareable",
  "target_keywords": ["keyword1", "keyword2", "keyword3"]
}
"""

def _build_user_prompt(exclusion_list: list[str], trends: list[str]) -> str:
    exclusions_block = (
        "\n".join(f"- {t}" for t in exclusion_list)
        if exclusion_list
        else "(none yet — this is the first run)"
    )
    trends_block = (
        "Currently trending related searches (optional inspiration only — don't force a bad fit):\n"
        + "\n".join(f"- {t}" for t in trends)
        if trends
        else "(trends unavailable)"
    )

    return f"""Generate ONE fresh, highly shareable facts/trivia topic.

DO NOT use any topic from this exclusion list (last 60 used topics):
{exclusions_block}

{trends_block}

Respond with ONLY the JSON object — nothing else."""


# ---------------------------------------------------------------------------
# Cost estimation (Haiku pricing as of 2024)
# ---------------------------------------------------------------------------

_HAIKU_INPUT_COST_PER_M = 0.25   # USD per million input tokens
_HAIKU_OUTPUT_COST_PER_M = 1.25  # USD per million output tokens

def _estimate_cost(usage: anthropic.types.Usage) -> float:
    return (
        usage.input_tokens * _HAIKU_INPUT_COST_PER_M / 1_000_000
        + usage.output_tokens * _HAIKU_OUTPUT_COST_PER_M / 1_000_000
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_topic(run_date: str | None = None) -> TopicOutput:
    """Generate and persist today's topic. Idempotent: returns cached result if already done."""
    if run_date is None:
        run_date = datetime.utcnow().strftime("%Y-%m-%d")

    # Idempotency: if topic.json already exists for today, load and return it
    run_dir = Path(__file__).parent.parent / "runs" / run_date
    topic_path = run_dir / "topic.json"
    if topic_path.exists():
        logger.info(f"Topic already generated for {run_date}, loading from cache.")
        return TopicOutput.model_validate_json(topic_path.read_text())

    init_db()
    config = _load_config()
    max_retries: int = config["llm"]["max_retries"]
    model: str = config["llm"]["model"]

    exclusion_list = get_recent_topics(limit=60)
    trends = _fetch_trends()

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        logger.info(f"Topic generation attempt {attempt}/{max_retries}")
        try:
            response = client.messages.create(
                model=model,
                max_tokens=512,
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        # Cache the static system prompt — it never changes between retries
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {
                        "role": "user",
                        "content": _build_user_prompt(exclusion_list, trends),
                    }
                ],
            )

            cost = _estimate_cost(response.usage)
            log_api_cost(run_date, "anthropic", cost)
            logger.info(
                f"Haiku usage — in:{response.usage.input_tokens} out:{response.usage.output_tokens} "
                f"cost:${cost:.5f}"
            )

            raw_text = response.content[0].text.strip()

            # Strip accidental markdown fences if the model adds them despite instructions
            raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
            raw_text = re.sub(r"\s*```$", "", raw_text)

            # Haiku occasionally returns 4 keywords — truncate silently
            data = json.loads(raw_text)
            if isinstance(data.get("target_keywords"), list):
                data["target_keywords"] = data["target_keywords"][:3]
            topic = TopicOutput.model_validate(data)
            logger.success(f"Topic generated: '{topic.topic}' [{topic.category}]")

            # Persist
            run_dir.mkdir(parents=True, exist_ok=True)
            topic_path.write_text(topic.model_dump_json(indent=2))
            save_topic(
                run_date=run_date,
                topic=topic.topic,
                category=topic.category,
                hook_angle=topic.hook_angle,
                keywords=topic.target_keywords,
            )
            return topic

        except (ValidationError, json.JSONDecodeError, ValueError) as exc:
            last_exc = exc
            logger.warning(f"Attempt {attempt} failed validation: {exc}")
        except anthropic.APIError as exc:
            last_exc = exc
            logger.warning(f"Attempt {attempt} API error: {exc}")

    raise RuntimeError(
        f"Topic generation failed after {max_retries} attempts. Last error: {last_exc}"
    )
