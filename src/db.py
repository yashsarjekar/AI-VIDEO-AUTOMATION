"""SQLite helpers — all DB access lives here."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Generator

DB_PATH = Path(__file__).parent.parent / "state.db"

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create all tables if they don't exist. Safe to call repeatedly."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS topics (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date    TEXT NOT NULL,
                topic       TEXT NOT NULL,
                category    TEXT NOT NULL,
                hook_angle  TEXT NOT NULL,
                keywords    TEXT NOT NULL,  -- JSON array stored as text
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE UNIQUE INDEX IF NOT EXISTS uq_topics_run_date ON topics(run_date);

            CREATE TABLE IF NOT EXISTS uploads (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date        TEXT NOT NULL,
                topic           TEXT NOT NULL,
                yt_video_id     TEXT,
                yt_url          TEXT,
                ig_media_id     TEXT,
                ig_permalink    TEXT,
                uploaded_at     TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE UNIQUE INDEX IF NOT EXISTS uq_uploads_run_date ON uploads(run_date);

            CREATE TABLE IF NOT EXISTS hashtag_usage (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                hashtag     TEXT NOT NULL,
                platform    TEXT NOT NULL,  -- 'youtube' | 'instagram'
                used_on     TEXT NOT NULL,  -- YYYY-MM-DD
                UNIQUE(hashtag, platform, used_on)
            );

            CREATE TABLE IF NOT EXISTS video_stats (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date            TEXT NOT NULL,
                platform            TEXT NOT NULL,
                video_id            TEXT NOT NULL,
                views               INTEGER DEFAULT 0,
                likes               INTEGER DEFAULT 0,
                comments            INTEGER DEFAULT 0,
                shares              INTEGER DEFAULT 0,
                watch_time_seconds  REAL,
                fetched_at          TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(video_id, platform, fetched_at)
            );

            CREATE TABLE IF NOT EXISTS api_cost_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date    TEXT NOT NULL,
                service     TEXT NOT NULL,  -- 'anthropic' | 'elevenlabs' | etc.
                cost_usd    REAL NOT NULL,
                logged_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------

def get_recent_topics(limit: int = 60) -> list[str]:
    """Return the last N topic strings to pass as exclusion list to LLM."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT topic FROM topics ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [r["topic"] for r in rows]


def save_topic(run_date: str, topic: str, category: str, hook_angle: str, keywords: list[str]) -> None:
    import json
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO topics (run_date, topic, category, hook_angle, keywords)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_date) DO UPDATE SET
                topic=excluded.topic,
                category=excluded.category,
                hook_angle=excluded.hook_angle,
                keywords=excluded.keywords
            """,
            (run_date, topic, category, hook_angle, json.dumps(keywords)),
        )


def get_topic_for_date(run_date: str) -> dict | None:
    import json
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM topics WHERE run_date = ?", (run_date,)
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["keywords"] = json.loads(d["keywords"])
    return d


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

def save_upload(
    run_date: str,
    topic: str,
    yt_video_id: str | None = None,
    yt_url: str | None = None,
    ig_media_id: str | None = None,
    ig_permalink: str | None = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO uploads (run_date, topic, yt_video_id, yt_url, ig_media_id, ig_permalink)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_date) DO UPDATE SET
                yt_video_id=COALESCE(excluded.yt_video_id, yt_video_id),
                yt_url=COALESCE(excluded.yt_url, yt_url),
                ig_media_id=COALESCE(excluded.ig_media_id, ig_media_id),
                ig_permalink=COALESCE(excluded.ig_permalink, ig_permalink)
            """,
            (run_date, topic, yt_video_id, yt_url, ig_media_id, ig_permalink),
        )


def get_upload_for_date(run_date: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM uploads WHERE run_date = ?", (run_date,)
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Hashtag rotation
# ---------------------------------------------------------------------------

def record_hashtag_usage(hashtags: list[str], platform: str, run_date: str) -> None:
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO hashtag_usage (hashtag, platform, used_on) VALUES (?, ?, ?)",
            [(h, platform, run_date) for h in hashtags],
        )


def get_hashtag_usage_last_n_days(platform: str, days: int = 7) -> dict[str, int]:
    """Return {hashtag: use_count} for hashtags used in the last N days."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT hashtag, COUNT(*) as cnt
            FROM hashtag_usage
            WHERE platform = ? AND used_on >= ?
            GROUP BY hashtag
            """,
            (platform, cutoff),
        ).fetchall()
    return {r["hashtag"]: r["cnt"] for r in rows}


# ---------------------------------------------------------------------------
# Video stats
# ---------------------------------------------------------------------------

def save_video_stats(
    run_date: str,
    platform: str,
    video_id: str,
    views: int,
    likes: int,
    comments: int,
    shares: int,
    watch_time_seconds: float | None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO video_stats
                (run_date, platform, video_id, views, likes, comments, shares, watch_time_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_date, platform, video_id, views, likes, comments, shares, watch_time_seconds),
        )


def get_stats_for_report(days: int = 7) -> list[dict]:
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT vs.*, u.topic
            FROM video_stats vs
            LEFT JOIN uploads u ON vs.run_date = u.run_date AND vs.platform = (
                CASE WHEN u.yt_video_id = vs.video_id THEN 'youtube' ELSE 'instagram' END
            )
            WHERE vs.run_date >= ?
            ORDER BY vs.run_date DESC, vs.platform
            """,
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# API cost tracking
# ---------------------------------------------------------------------------

def log_api_cost(run_date: str, service: str, cost_usd: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO api_cost_log (run_date, service, cost_usd) VALUES (?, ?, ?)",
            (run_date, service, cost_usd),
        )


def get_total_cost_for_date(run_date: str) -> float:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) as total FROM api_cost_log WHERE run_date = ?",
            (run_date,),
        ).fetchone()
    return float(row["total"])
