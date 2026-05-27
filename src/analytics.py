"""Weekly analytics: pull stats from YouTube + Instagram, write SQLite + markdown report."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import requests
import yaml
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from loguru import logger

from .db import get_stats_for_report, get_upload_for_date, init_db, save_video_stats

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
_IG_GRAPH_BASE = "https://graph.facebook.com/v19.0"
_YT_TOKEN_URI = "https://oauth2.googleapis.com/token"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# YouTube stats
# ---------------------------------------------------------------------------

def _build_yt_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        token_uri=_YT_TOKEN_URI,
    )
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _fetch_youtube_stats(video_ids: list[str]) -> dict[str, dict]:
    """Return {video_id: statistics_dict} for a batch of IDs."""
    if not video_ids:
        return {}
    youtube = _build_yt_service()
    # YouTube API allows up to 50 IDs per request
    results: dict[str, dict] = {}
    for batch_start in range(0, len(video_ids), 50):
        batch = video_ids[batch_start : batch_start + 50]
        response = (
            youtube.videos()
            .list(part="statistics", id=",".join(batch))
            .execute()
        )
        for item in response.get("items", []):
            results[item["id"]] = item.get("statistics", {})
    return results


# ---------------------------------------------------------------------------
# Instagram stats
# ---------------------------------------------------------------------------

def _fetch_ig_insights(media_id: str, token: str) -> dict:
    """Return metrics dict for a single Instagram media object."""
    # Reels support: plays, reach, likes, comments, shares, saved
    metrics = "plays,reach,likes,comments,shares,saved"
    try:
        resp = requests.get(
            f"{_IG_GRAPH_BASE}/{media_id}/insights",
            params={"metric": metrics, "access_token": token},
            timeout=15,
        )
        resp.raise_for_status()
        data: dict[str, int] = {}
        for item in resp.json().get("data", []):
            data[item["name"]] = item.get("values", [{}])[0].get("value", 0)
        return data
    except Exception as exc:
        logger.warning(f"IG insights fetch failed for {media_id}: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Main analytics pull
# ---------------------------------------------------------------------------

def run_analytics(days: int = 7) -> Path:
    """Pull stats for all videos uploaded in the last `days` days.

    Saves rows to SQLite and writes a markdown report.
    Returns the report path.
    """
    init_db()
    config = _load_config()
    yt_enabled: bool = config["upload"]["youtube"].get("enabled", True)
    ig_enabled: bool = config["upload"]["instagram"].get("enabled", True)

    today = datetime.utcnow()
    report_date = today.strftime("%Y-%m-%d")

    # Collect uploads from the last N days
    yt_video_ids: list[str] = []
    ig_media_ids: list[str] = []
    run_dates: list[str] = []

    for offset in range(days):
        date_str = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        row = get_upload_for_date(date_str)
        if row is None:
            continue
        run_dates.append(date_str)
        if row.get("yt_video_id"):
            yt_video_ids.append(row["yt_video_id"])
        if row.get("ig_media_id"):
            ig_media_ids.append(row["ig_media_id"])

    logger.info(
        f"Analytics: {len(run_dates)} runs, "
        f"{len(yt_video_ids)} YT videos, {len(ig_media_ids)} IG reels"
    )

    # Fetch YouTube stats
    yt_stats: dict[str, dict] = {}
    if yt_enabled and yt_video_ids:
        try:
            yt_stats = _fetch_youtube_stats(yt_video_ids)
            logger.info(f"YouTube stats fetched for {len(yt_stats)} videos.")
        except Exception as exc:
            logger.warning(f"YouTube stats fetch failed: {exc}")

    # Fetch Instagram stats
    ig_token = os.environ.get("IG_ACCESS_TOKEN", "")
    ig_stats: dict[str, dict] = {}
    if ig_enabled and ig_media_ids and ig_token:
        for media_id in ig_media_ids:
            ig_stats[media_id] = _fetch_ig_insights(media_id, ig_token)
        logger.info(f"Instagram stats fetched for {len(ig_stats)} reels.")

    # Save to SQLite
    for offset in range(days):
        date_str = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        row = get_upload_for_date(date_str)
        if row is None:
            continue

        if row.get("yt_video_id") and row["yt_video_id"] in yt_stats:
            s = yt_stats[row["yt_video_id"]]
            save_video_stats(
                run_date=date_str,
                platform="youtube",
                video_id=row["yt_video_id"],
                views=int(s.get("viewCount", 0)),
                likes=int(s.get("likeCount", 0)),
                comments=int(s.get("commentCount", 0)),
                shares=0,
                watch_time_seconds=None,
            )

        if row.get("ig_media_id") and row["ig_media_id"] in ig_stats:
            s = ig_stats[row["ig_media_id"]]
            save_video_stats(
                run_date=date_str,
                platform="instagram",
                video_id=row["ig_media_id"],
                views=int(s.get("plays", s.get("reach", 0))),
                likes=int(s.get("likes", 0)),
                comments=int(s.get("comments", 0)),
                shares=int(s.get("shares", 0)),
                watch_time_seconds=None,
            )

    # Generate report
    report_path = _write_report(report_date, days)
    return report_path


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

def _write_report(report_date: str, days: int) -> Path:
    rows = get_stats_for_report(days=days)

    lines = [
        f"# Weekly Stats Report — {report_date}",
        f"_Covers the last {days} days. Generated by analytics.py._",
        "",
        "## Videos",
        "",
        "| Date | Topic | Platform | Views | Likes | Comments | Shares |",
        "|------|-------|----------|------:|------:|---------:|-------:|",
    ]

    total_views = total_likes = total_comments = 0
    for r in rows:
        topic_short = (r.get("topic") or "—")[:40]
        lines.append(
            f"| {r['run_date']} | {topic_short} | {r['platform']} "
            f"| {r['views']:,} | {r['likes']:,} | {r['comments']:,} | {r['shares']:,} |"
        )
        total_views += r["views"]
        total_likes += r["likes"]
        total_comments += r["comments"]

    lines += [
        "",
        "## Totals",
        "",
        f"- **Views:** {total_views:,}",
        f"- **Likes:** {total_likes:,}",
        f"- **Comments:** {total_comments:,}",
        "",
    ]

    if rows:
        best = max(rows, key=lambda r: r["views"])
        topic_short = (best.get("topic") or "—")[:60]
        lines += [
            "## Top Performer",
            "",
            f"**{best['run_date']} / {best['platform']}** — {topic_short}",
            f"- Views: {best['views']:,} | Likes: {best['likes']:,} "
            f"| Comments: {best['comments']:,}",
            "",
        ]

    reports_dir = Path(__file__).parent.parent / "reports"
    reports_dir.mkdir(exist_ok=True)
    report_path = reports_dir / f"{report_date}-stats.md"
    report_path.write_text("\n".join(lines))
    logger.success(f"Report written: {report_path.name}")
    return report_path


if __name__ == "__main__":
    run_analytics()
