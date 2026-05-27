"""Stage 8 — YouTube and Instagram upload with token rotation."""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from loguru import logger

from .db import save_upload
from .schemas import (
    InstagramUploadResult,
    MetadataOutput,
    UploadResult,
    YouTubeUploadResult,
)
from .storage import delete_video, upload_video

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

def _load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

_YT_TOKEN_URI = "https://oauth2.googleapis.com/token"
_YT_MAX_RETRIES = 5
_YT_RETRIABLE_CODES = {500, 502, 503, 504}
_YT_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB


def _build_yt_service() -> tuple["googleapiclient.discovery.Resource", Credentials]:
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        token_uri=_YT_TOKEN_URI,
    )
    creds.refresh(Request())
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    return service, creds


def _rotate_yt_token_if_needed(original_refresh: str, creds: Credentials) -> None:
    """If the OAuth flow issued a new refresh token, persist it as a GitHub secret."""
    new_token = creds.refresh_token
    if new_token and new_token != original_refresh:
        logger.info("YouTube refresh token rotated — updating GitHub secret.")
        result = subprocess.run(
            ["gh", "secret", "set", "YOUTUBE_REFRESH_TOKEN"],
            input=new_token,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            logger.warning(f"Failed to rotate YT secret: {result.stderr.strip()}")
        else:
            logger.success("YOUTUBE_REFRESH_TOKEN secret updated.")


def _upload_to_youtube(
    video_path: Path, metadata: MetadataOutput, config: dict
) -> YouTubeUploadResult:
    privacy = config["upload"]["youtube"].get("privacy", "public")
    original_refresh = os.environ["YOUTUBE_REFRESH_TOKEN"]

    youtube, creds = _build_yt_service()

    body = {
        "snippet": {
            "title": metadata.youtube.title,
            "description": metadata.youtube.description,
            "tags": metadata.youtube.tags,
            "categoryId": str(metadata.youtube.category_id),
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=_YT_CHUNK_SIZE,
    )
    insert_request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    last_exc: Exception | None = None

    for attempt in range(_YT_MAX_RETRIES):
        try:
            logger.info(f"YouTube upload attempt {attempt + 1}/{_YT_MAX_RETRIES}")
            while response is None:
                status, response = insert_request.next_chunk()
                if status:
                    logger.info(f"YouTube upload progress: {int(status.progress() * 100)}%")
            break
        except HttpError as exc:
            if exc.resp.status in _YT_RETRIABLE_CODES:
                wait = 2 ** attempt
                logger.warning(f"YouTube {exc.resp.status}, retrying in {wait}s")
                time.sleep(wait)
                last_exc = exc
                response = None  # reset for retry
            else:
                raise
    else:
        raise RuntimeError(f"YouTube upload failed after {_YT_MAX_RETRIES} retries: {last_exc}")

    video_id: str = response["id"]
    url = f"https://www.youtube.com/shorts/{video_id}"
    logger.success(f"YouTube upload complete: {url}")

    _rotate_yt_token_if_needed(original_refresh, creds)

    return YouTubeUploadResult(
        video_id=video_id,
        url=url,
        privacy_status=privacy,
    )


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------

_IG_GRAPH_BASE = "https://graph.facebook.com/v19.0"
_IG_POLL_INTERVAL = 8    # seconds between status polls
_IG_POLL_MAX = 30        # max polling attempts (~4 minutes)


def _ig_user_id() -> str:
    uid = os.environ.get("IG_USER_ID", "")
    if not uid:
        raise ValueError("IG_USER_ID environment variable is not set.")
    return uid


def _ig_token() -> str:
    token = os.environ.get("IG_ACCESS_TOKEN", "")
    if not token:
        raise ValueError("IG_ACCESS_TOKEN environment variable is not set.")
    return token


def _poll_ig_status(creation_id: str, token: str) -> str:
    """Poll until status_code is FINISHED or ERROR. Returns final status_code."""
    for attempt in range(_IG_POLL_MAX):
        resp = requests.get(
            f"{_IG_GRAPH_BASE}/{creation_id}",
            params={"fields": "status_code,status", "access_token": token},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status_code", "IN_PROGRESS")
        logger.info(f"Instagram container status ({attempt + 1}/{_IG_POLL_MAX}): {status}")
        if status == "FINISHED":
            return status
        if status == "ERROR":
            error_detail = data.get("status", "no detail")
            raise RuntimeError(f"Instagram container processing failed: {error_detail}")
        time.sleep(_IG_POLL_INTERVAL)

    raise RuntimeError(
        f"Instagram container did not finish processing after "
        f"{_IG_POLL_MAX * _IG_POLL_INTERVAL}s."
    )


def _post_ig_first_comment(media_id: str, comment: str, token: str) -> None:
    if not comment.strip():
        return
    resp = requests.post(
        f"{_IG_GRAPH_BASE}/{media_id}/comments",
        params={"message": comment, "access_token": token},
        timeout=15,
    )
    if resp.status_code == 200:
        logger.success("Instagram first-comment (hashtag block) posted.")
    else:
        logger.warning(f"First-comment post failed: {resp.status_code} {resp.text[:200]}")


def _get_ig_permalink(media_id: str, token: str) -> str | None:
    try:
        resp = requests.get(
            f"{_IG_GRAPH_BASE}/{media_id}",
            params={"fields": "permalink", "access_token": token},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("permalink")
    except Exception as exc:
        logger.warning(f"Could not fetch IG permalink: {exc}")
        return None


def _upload_to_instagram(
    video_url: str, metadata: MetadataOutput
) -> InstagramUploadResult:
    user_id = _ig_user_id()
    token = _ig_token()

    # Step 1 — create media container
    logger.info("Creating Instagram Reels media container …")
    caption_text = metadata.instagram.caption
    # Append the 5 high-volume hashtags already in the caption structure.
    # (The LLM puts them in the caption; we keep that as-is.)

    resp = requests.post(
        f"{_IG_GRAPH_BASE}/{user_id}/media",
        params={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption_text,
            "share_to_feed": "true",
            "access_token": token,
        },
        timeout=30,
    )
    resp.raise_for_status()
    creation_id: str = resp.json()["id"]
    logger.info(f"Instagram container created: {creation_id}")

    # Step 2 — poll until processing finishes
    _poll_ig_status(creation_id, token)

    # Step 3 — publish
    logger.info("Publishing Instagram Reel …")
    pub_resp = requests.post(
        f"{_IG_GRAPH_BASE}/{user_id}/media_publish",
        params={"creation_id": creation_id, "access_token": token},
        timeout=30,
    )
    pub_resp.raise_for_status()
    media_id: str = pub_resp.json()["id"]
    logger.success(f"Instagram Reel published: media_id={media_id}")

    permalink = _get_ig_permalink(media_id, token)

    # Step 4 — post hashtag block as first comment
    _post_ig_first_comment(media_id, metadata.instagram_first_comment, token)

    return InstagramUploadResult(media_id=media_id, permalink=permalink)


# ---------------------------------------------------------------------------
# Instagram token refresh
# ---------------------------------------------------------------------------

def refresh_ig_token_if_needed(days_threshold: int = 15) -> bool:
    """Refresh the IG long-lived token if it expires within `days_threshold` days.

    Returns True if the token was refreshed (caller should update the GitHub secret).
    """
    token = _ig_token()
    app_id = os.environ.get("FB_APP_ID", "")
    app_secret = os.environ.get("FB_APP_SECRET", "")
    if not app_id or not app_secret:
        logger.warning("FB_APP_ID / FB_APP_SECRET not set — skipping IG token check.")
        return False

    try:
        # Inspect the current token
        debug_resp = requests.get(
            f"{_IG_GRAPH_BASE}/debug_token",
            params={
                "input_token": token,
                "access_token": f"{app_id}|{app_secret}",
            },
            timeout=10,
        )
        debug_resp.raise_for_status()
        data = debug_resp.json().get("data", {})
        expires_at = data.get("expires_at", 0)

        if expires_at == 0:
            logger.info("IG token has no expiry (permanent?) — skipping refresh.")
            return False

        expires_dt = datetime.fromtimestamp(expires_at, tz=timezone.utc)
        days_left = (expires_dt - datetime.now(tz=timezone.utc)).days
        logger.info(f"IG token expires in {days_left} days ({expires_dt.date()})")

        if days_left > days_threshold:
            return False

        logger.info("Refreshing IG long-lived token …")
        refresh_resp = requests.get(
            f"{_IG_GRAPH_BASE}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": token,
            },
            timeout=10,
        )
        refresh_resp.raise_for_status()
        new_token = refresh_resp.json()["access_token"]

        result = subprocess.run(
            ["gh", "secret", "set", "IG_ACCESS_TOKEN"],
            input=new_token,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            logger.warning(f"Failed to update IG_ACCESS_TOKEN secret: {result.stderr.strip()}")
            return False

        # Update the current process environment so the rest of this run uses the new token
        os.environ["IG_ACCESS_TOKEN"] = new_token
        logger.success("IG_ACCESS_TOKEN refreshed and GitHub secret updated.")
        return True

    except Exception as exc:
        logger.warning(f"IG token refresh check failed (non-fatal): {exc}")
        return False


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def upload_all(
    run_date: str | None = None,
    metadata: MetadataOutput | None = None,
) -> UploadResult:
    """Upload final.mp4 to YouTube and Instagram. Idempotent (skips if already uploaded)."""
    if run_date is None:
        run_date = datetime.utcnow().strftime("%Y-%m-%d")

    run_dir = Path(__file__).parent.parent / "runs" / run_date
    video_path = run_dir / "final.mp4"

    if not video_path.exists():
        raise FileNotFoundError(f"final.mp4 not found for {run_date}.")

    # Load metadata if not provided
    if metadata is None:
        mp = run_dir / "metadata.json"
        if not mp.exists():
            raise FileNotFoundError(f"metadata.json not found for {run_date}.")
        metadata = MetadataOutput.model_validate_json(mp.read_text())

    config = _load_config()
    yt_enabled: bool = config["upload"]["youtube"].get("enabled", True)
    ig_enabled: bool = config["upload"]["instagram"].get("enabled", True)

    yt_result: YouTubeUploadResult | None = None
    ig_result: InstagramUploadResult | None = None
    r2_key: str | None = None

    # --- YouTube upload ---
    if yt_enabled:
        try:
            yt_result = _upload_to_youtube(video_path, metadata, config)
        except Exception as exc:
            logger.error(f"YouTube upload failed: {exc}")
            raise

    # --- R2 upload (required for Instagram) ---
    if ig_enabled:
        try:
            r2_key, video_url = upload_video(video_path, run_date)
        except Exception as exc:
            logger.error(f"R2 upload failed — cannot proceed with Instagram: {exc}")
            raise

        # --- Instagram upload ---
        try:
            ig_result = _upload_to_instagram(video_url, metadata)
        except Exception as exc:
            logger.error(f"Instagram upload failed: {exc}")
            raise
        finally:
            # Always clean up R2, even on failure
            if r2_key:
                try:
                    delete_video(r2_key)
                except Exception as del_exc:
                    logger.warning(f"R2 cleanup failed (non-fatal): {del_exc}")

    # --- Persist to DB ---
    save_upload(
        run_date=run_date,
        topic=metadata.youtube.title,
        yt_video_id=yt_result.video_id if yt_result else None,
        yt_url=yt_result.url if yt_result else None,
        ig_media_id=ig_result.media_id if ig_result else None,
        ig_permalink=ig_result.permalink if ig_result else None,
    )

    return UploadResult(
        run_date=run_date,
        topic=metadata.youtube.title,
        youtube=yt_result,
        instagram=ig_result,
    )
