"""Stage 2 — Script generation via Claude Haiku with fact-accuracy guardrail."""

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

from .db import log_api_cost
from .schemas import ScriptOutput, TopicOutput

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

def _load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Living-person image prompt validator
# ---------------------------------------------------------------------------

# Patterns that suggest a real person depiction
_PERSON_DEPICTION_PATTERNS = [
    re.compile(r"\bphoto(?:graph)? of\b", re.IGNORECASE),
    re.compile(r"\bportrait of\b", re.IGNORECASE),
    re.compile(r"\bpicture of\b", re.IGNORECASE),
    re.compile(r"\bimage of\b.*\b[A-Z][a-z]+\s[A-Z][a-z]+\b"),  # "image of John Smith"
    # Two consecutive capitalized words following common trigger phrases
    re.compile(r"\b(?:show|depict|render|draw)\b.*\b[A-Z][a-z]+\s[A-Z][a-z]+\b", re.IGNORECASE),
]

def _validate_visual_prompt(prompt: str, scene_index: int) -> str:
    """Raise ValueError if the prompt depicts a real person."""
    for pattern in _PERSON_DEPICTION_PATTERNS:
        if pattern.search(prompt):
            raise ValueError(
                f"Scene {scene_index} visual_prompt may depict a real person (matched pattern "
                f"'{pattern.pattern}'): '{prompt[:80]}...'"
            )
    return prompt


def _validate_all_visual_prompts(script: ScriptOutput) -> None:
    for i, scene in enumerate(script.scenes, start=1):
        _validate_visual_prompt(scene.visual_prompt, i)


# ---------------------------------------------------------------------------
# Claude prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a viral short-form video scriptwriter. You write tight, punchy scripts for 45-second \
YouTube Shorts and Instagram Reels about fascinating facts and trivia.

TARGET AUDIENCE: Curious adults 18–35, English-speaking, mobile-first.
TONE: Conversational, slightly playful, never condescending. Sound like a smart friend \
who just learned something amazing and can't wait to share it.

=== HOOK RULES (non-negotiable) ===
- NEVER start with "Did you know...", "Today we'll learn...", "In this video...", \
"Welcome back...", or any greeting.
- FORCE a pattern interrupt: open with a contradiction, a specific number, a "wait, what?" \
statement, or a bold counterintuitive claim.
- Hook must create a curiosity gap that COMPELS the viewer to keep watching.
- Hook must be ≤12 words.

=== SCENE RULES ===
- 6–8 scenes total.
- Each scene line ≤15 words.
- One clear, punchy idea per line — no run-ons.
- Build momentum: each scene should raise the stakes or deepen the surprise.
- Last content scene delivers the satisfying payoff.

=== VISUAL PROMPT RULES ===
- Describe vivid, concrete, cinematically interesting imagery.
- NEVER include real people — living OR historical. No faces, no portraits.
- Describe objects, environments, phenomena, abstract representations, or symbolic imagery.
- Optimized for vertical 1080×1920 framing.
- Be specific: "a glowing blue deep-sea jellyfish drifting through black water, bioluminescent \
tendrils trailing" not "a jellyfish".

=== FACT-ACCURACY GUARDRAIL (critical) ===
- Only include claims you are HIGHLY CONFIDENT are true and well-documented.
- If the topic requires uncertain, disputed, or frequently-contested claims, set \
"uncertain_claims": true and still produce a best-effort script. The pipeline will discard it \
and re-roll the topic.
- Do NOT hallucinate statistics, dates, or names. If you don't know a specific number \
with confidence, express the idea qualitatively instead.

=== CTA RULES ===
- One specific, answerable question — something viewers genuinely want to debate or answer.
- NEVER "What do you think?", "Let me know in the comments", or "Share your thoughts".
- Good examples: "Which of these facts surprised you more — the timing or the mechanism?"
- "If you could only keep one sense, which would it be and why?"

You MUST respond with ONLY valid JSON matching this exact schema — no prose, no markdown fences:
{
  "hook": "string, ≤12 words",
  "scenes": [
    {
      "line": "string, ≤15 words",
      "visual_prompt": "string, vivid concrete imagery, no real people"
    }
  ],
  "cta": "string, specific answerable question",
  "uncertain_claims": false
}
"""

def _build_user_prompt(topic: TopicOutput, config: dict) -> str:
    scenes_min = config["video"]["scenes_min"]
    scenes_max = config["video"]["scenes_max"]
    duration = config["video"]["duration_target_seconds"]

    return f"""Write a complete script for this topic:

TOPIC: {topic.topic}
CATEGORY: {topic.category}
HOOK ANGLE: {topic.hook_angle}
TARGET KEYWORDS: {", ".join(topic.target_keywords)}

Requirements:
- {scenes_min}–{scenes_max} scenes (target ~{duration} seconds total, ~6–7 sec per scene)
- Each scene line narrated in natural spoken English — not bullet points, not headlines
- Visual prompts must paint a specific, concrete image suitable for AI image generation
- The script should flow as a coherent narrative arc: hook → build → reveal → payoff → CTA

Respond with ONLY the JSON object."""


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

_HAIKU_INPUT_COST_PER_M = 0.25
_HAIKU_OUTPUT_COST_PER_M = 1.25

def _estimate_cost(usage: anthropic.types.Usage) -> float:
    return (
        usage.input_tokens * _HAIKU_INPUT_COST_PER_M / 1_000_000
        + usage.output_tokens * _HAIKU_OUTPUT_COST_PER_M / 1_000_000
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

MAX_TOPIC_REROLLS = 2

def generate_script(
    run_date: str | None = None,
    topic: TopicOutput | None = None,
    *,
    _reroll_count: int = 0,
) -> tuple[ScriptOutput, TopicOutput]:
    """Generate and persist the script for today.

    Returns (script, topic) — topic may differ from the input if a re-roll occurred.
    Idempotent: if script.json already exists, loads and returns it.
    """
    if run_date is None:
        run_date = datetime.utcnow().strftime("%Y-%m-%d")

    run_dir = Path(__file__).parent.parent / "runs" / run_date
    script_path = run_dir / "script.json"

    # Idempotency
    if script_path.exists():
        logger.info(f"Script already generated for {run_date}, loading from cache.")
        cached = ScriptOutput.model_validate_json(script_path.read_text())
        # Also load the topic so the caller gets a consistent pair
        topic_path = run_dir / "topic.json"
        if topic is None and topic_path.exists():
            topic = TopicOutput.model_validate_json(topic_path.read_text())
        return cached, topic  # type: ignore[return-value]

    # Load topic if not provided
    if topic is None:
        topic_path = run_dir / "topic.json"
        if not topic_path.exists():
            raise FileNotFoundError(
                f"topic.json not found for {run_date}. Run topic_generator first."
            )
        topic = TopicOutput.model_validate_json(topic_path.read_text())

    config = _load_config()
    max_retries: int = config["llm"]["max_retries"]
    model: str = config["llm"]["model"]

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        logger.info(f"Script generation attempt {attempt}/{max_retries} for topic: '{topic.topic}'")
        try:
            response = client.messages.create(
                model=model,
                max_tokens=1024,
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
                        "content": _build_user_prompt(topic, config),
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

            script = ScriptOutput.model_validate_json(raw_text)

            # Validate visual prompts for person depictions
            _validate_all_visual_prompts(script)

            # Handle uncertain-claims re-roll
            if script.uncertain_claims:
                if _reroll_count >= MAX_TOPIC_REROLLS:
                    raise RuntimeError(
                        f"Topic '{topic.topic}' flagged uncertain_claims and re-roll limit "
                        f"({MAX_TOPIC_REROLLS}) reached. Aborting."
                    )
                logger.warning(
                    f"Script flagged uncertain_claims for topic '{topic.topic}'. "
                    f"Re-rolling topic (attempt {_reroll_count + 1}/{MAX_TOPIC_REROLLS})."
                )
                # Import here to avoid circular imports at module load
                from .topic_generator import generate_topic  # noqa: PLC0415

                # Remove the cached topic.json so topic_generator generates a fresh one
                topic_path = run_dir / "topic.json"
                if topic_path.exists():
                    topic_path.unlink()

                new_topic = generate_topic(run_date)
                return generate_script(
                    run_date=run_date,
                    topic=new_topic,
                    _reroll_count=_reroll_count + 1,
                )

            logger.success(
                f"Script generated: {len(script.scenes)} scenes, "
                f"hook='{script.hook[:50]}...'"
            )

            run_dir.mkdir(parents=True, exist_ok=True)
            script_path.write_text(script.model_dump_json(indent=2))
            return script, topic

        except (ValidationError, json.JSONDecodeError, ValueError) as exc:
            last_exc = exc
            logger.warning(f"Attempt {attempt} failed validation: {exc}")
        except anthropic.APIError as exc:
            last_exc = exc
            logger.warning(f"Attempt {attempt} API error: {exc}")

    raise RuntimeError(
        f"Script generation failed after {max_retries} attempts for topic '{topic.topic}'. "
        f"Last error: {last_exc}"
    )
