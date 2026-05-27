"""Pipeline orchestrator — runs stages 1–8 sequentially."""

from __future__ import annotations

import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

import yaml
from loguru import logger

from .caption_generator import generate_captions
from .db import get_total_cost_for_date, init_db
from .metadata_generator import generate_metadata
from .notifications import notify_cost_alert, notify_failure, notify_success
from .script_writer import generate_script
from .topic_generator import generate_topic
from .uploader import refresh_ig_token_if_needed, upload_all
from .video_builder import build_video
from .visual_generator import generate_visuals
from .voice_generator import generate_voices

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
_REPO_ROOT = Path(__file__).parent.parent


def _load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(run_dir: Path) -> None:
    logger.remove()
    fmt = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{line} — {message}"
    logger.add(sys.stdout, format=fmt, level="INFO", colorize=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        str(run_dir / "run.log"),
        format=fmt,
        level="DEBUG",
        rotation="10 MB",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Git state persistence
# ---------------------------------------------------------------------------

def _commit_state(run_date: str) -> None:
    """Commit state.db and today's JSON/log outputs back to the repo."""
    run_dir = _REPO_ROOT / "runs" / run_date

    files: list[str] = ["state.db"]
    for pattern in ("*.json", "*.log"):
        files.extend(
            str(p.relative_to(_REPO_ROOT)) for p in run_dir.glob(pattern) if p.exists()
        )

    try:
        subprocess.run(["git", "add"] + files, cwd=str(_REPO_ROOT), check=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(_REPO_ROOT),
        )
        if result.returncode == 0:
            logger.info("Nothing to commit — state unchanged.")
            return

        subprocess.run(
            ["git", "commit", "-m", f"Daily run {run_date} [skip ci]"],
            cwd=str(_REPO_ROOT),
            check=True,
        )
        subprocess.run(["git", "push"], cwd=str(_REPO_ROOT), check=True)
        logger.success("State committed and pushed.")
    except subprocess.CalledProcessError as exc:
        logger.warning(f"Git commit/push failed (non-fatal): {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    run_date = datetime.utcnow().strftime("%Y-%m-%d")
    run_dir = _REPO_ROOT / "runs" / run_date
    _setup_logging(run_dir)

    logger.info(f"=== Daily run starting: {run_date} ===")
    init_db()
    config = _load_config()
    cost_threshold: float = config.get("costs", {}).get("daily_alert_threshold_usd", 0.10)

    current_stage = "init"
    try:
        # Stage 1 — Topic
        current_stage = "topic_generator"
        logger.info("--- Stage 1: topic_generator ---")
        topic = generate_topic(run_date)

        # Stage 2 — Script (may re-roll topic)
        current_stage = "script_writer"
        logger.info("--- Stage 2: script_writer ---")
        script, topic = generate_script(run_date, topic)

        # Stage 3 — Visuals
        current_stage = "visual_generator"
        logger.info("--- Stage 3: visual_generator ---")
        generate_visuals(run_date, script)

        # Stage 4 — Voice
        current_stage = "voice_generator"
        logger.info("--- Stage 4: voice_generator ---")
        generate_voices(run_date, script)

        # Stage 5 — Captions
        current_stage = "caption_generator"
        logger.info("--- Stage 5: caption_generator ---")
        captions = generate_captions(run_date)

        # Stage 6 — Video assembly
        current_stage = "video_builder"
        logger.info("--- Stage 6: video_builder ---")
        build_video(run_date, script, captions)

        # Stage 7 — Metadata
        current_stage = "metadata_generator"
        logger.info("--- Stage 7: metadata_generator ---")
        metadata = generate_metadata(run_date, topic, script)

        # Stage 8 — Upload
        current_stage = "uploader"
        logger.info("--- Stage 8: uploader ---")
        result = upload_all(run_date, metadata)

    except Exception:
        tb = traceback.format_exc()
        logger.error(f"Stage '{current_stage}' failed:\n{tb}")
        notify_failure(run_date, current_stage, tb)
        sys.exit(1)

    # Cost check
    total_cost = get_total_cost_for_date(run_date)
    logger.info(f"Total estimated API cost for {run_date}: ${total_cost:.4f}")
    if total_cost > cost_threshold:
        notify_cost_alert(run_date, total_cost, cost_threshold)

    # IG token rotation (best-effort, non-fatal)
    try:
        refresh_ig_token_if_needed()
    except Exception as exc:
        logger.warning(f"IG token check failed (non-fatal): {exc}")

    # Persist state
    _commit_state(run_date)

    # Success notification
    notify_success(
        run_date=run_date,
        yt_url=result.youtube.url if result.youtube else None,
        ig_permalink=result.instagram.permalink if result.instagram else None,
        total_cost=total_cost,
    )

    logger.info(f"=== Daily run complete: {run_date} ===")


if __name__ == "__main__":
    main()
