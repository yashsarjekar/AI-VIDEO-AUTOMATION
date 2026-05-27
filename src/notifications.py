"""Telegram notifications for failures, cost alerts, and success."""

from __future__ import annotations

import os

import requests
from loguru import logger

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_MAX_MSG_LEN = 4096


def _send(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing).")
        return
    try:
        resp = requests.post(
            _TELEGRAM_API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": message[:_MAX_MSG_LEN],
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as exc:
        # Notification failures must never crash the pipeline
        logger.warning(f"Telegram notification failed (non-fatal): {exc}")


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
    lines = [f"<b>Daily run SUCCESS</b>", f"Date: {run_date}"]
    if yt_url:
        lines.append(f"YouTube: {yt_url}")
    if ig_permalink:
        lines.append(f"Instagram: {ig_permalink}")
    lines.append(f"Cost: ${total_cost:.4f}")
    _send("\n".join(lines))
