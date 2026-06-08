"""Telegram notifications for failures, cost alerts, and success."""

from __future__ import annotations

import os
from pathlib import Path

import requests
from loguru import logger

_TELEGRAM_BASE = "https://api.telegram.org/bot{token}"
_MAX_MSG_LEN = 4096
_MAX_VIDEO_MB = 50


def _token_and_chat() -> tuple[str, str]:
    return os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")


def _send(message: str) -> None:
    token, chat_id = _token_and_chat()
    if not token or not chat_id:
        logger.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing).")
        return
    try:
        resp = requests.post(
            f"{_TELEGRAM_BASE.format(token=token)}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message[:_MAX_MSG_LEN],
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.warning(f"Telegram notification failed (non-fatal): {exc}")


def _send_video(video_path: Path, caption: str) -> None:
    token, chat_id = _token_and_chat()
    if not token or not chat_id:
        return
    size_mb = video_path.stat().st_size / (1024 * 1024)
    if size_mb > _MAX_VIDEO_MB:
        logger.warning(f"Video {size_mb:.1f} MB exceeds Telegram {_MAX_VIDEO_MB} MB limit — skipping video send.")
        return
    try:
        with video_path.open("rb") as f:
            resp = requests.post(
                f"{_TELEGRAM_BASE.format(token=token)}/sendVideo",
                data={
                    "chat_id": chat_id,
                    "caption": caption[:1024],
                    "supports_streaming": "true",
                },
                files={"video": (video_path.name, f, "video/mp4")},
                timeout=120,
            )
        resp.raise_for_status()
        logger.success("Video sent to Telegram.")
    except Exception as exc:
        logger.warning(f"Telegram video send failed (non-fatal): {exc}")


def notify_failure(run_date: str, stage: str, error: str) -> None:
    _send(
        f"<b>Daily run FAILED</b>\n"
        f"Date: {run_date}\n"
        f"Stage: <code>{stage}</code>\n"
        f"Error:\n<pre>{error[:800]}</pre>"
    )


def notify_cost_alert(run_date: str, total_cost: float, threshold: float) -> None:
    _send(
        f"<b>Cost Alert</b>\n"
        f"Date: {run_date}\n"
        f"Run cost: <b>${total_cost:.4f}</b> (threshold ${threshold:.2f})\n"
        "Check API usage dashboards."
    )


def notify_success(
    run_date: str,
    yt_url: str | None,
    ig_permalink: str | None,
    total_cost: float,
) -> None:
    lines = ["<b>Daily run SUCCESS</b>", f"Date: {run_date}"]
    if yt_url:
        lines.append(f"YouTube: {yt_url}")
    if ig_permalink:
        lines.append(f"Instagram: {ig_permalink}")
    lines.append(f"Cost: ${total_cost:.4f}")
    _send("\n".join(lines))


def notify_video_ready(
    run_date: str,
    metadata: object,  # MetadataOutput — avoid circular import
    video_path: Path,
    total_cost: float,
) -> None:
    """Send video + copy-pasteable metadata to Telegram for manual upload."""
    yt = metadata.youtube  # type: ignore[attr-defined]

    # Message 1 — title (copy-paste ready)
    _send(
        f"<b>Video ready for manual upload</b>\n"
        f"Date: {run_date} | Cost: ${total_cost:.4f}\n\n"
        f"<b>TITLE</b> (copy-paste):\n"
        f"<code>{yt.title}</code>"
    )

    # Message 2 — description (copy-paste ready)
    _send(f"<b>DESCRIPTION</b> (copy-paste):\n\n{yt.description[:_MAX_MSG_LEN - 50]}")

    # Message 3 — tags (copy-paste ready, comma-separated for YouTube)
    tags_str = ", ".join(yt.tags)
    _send(f"<b>TAGS</b> (copy-paste):\n<code>{tags_str}</code>")

    # Message 4 — video file
    _send_video(video_path, caption=f"{yt.title}\n\n{run_date}")
