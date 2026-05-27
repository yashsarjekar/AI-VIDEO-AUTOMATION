"""R2 storage helper — upload final.mp4 and get a presigned public URL."""

from __future__ import annotations

import os
from pathlib import Path

import boto3
from botocore.config import Config
from loguru import logger

# Presigned URL valid for 24 hours — plenty of time for Instagram processing
_PRESIGN_EXPIRES = 86_400


def _get_client() -> "boto3.client":
    account_id = os.environ["R2_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY"],
        aws_secret_access_key=os.environ["R2_SECRET_KEY"],
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def upload_video(video_path: Path, run_date: str) -> tuple[str, str]:
    """Upload final.mp4 to R2. Returns (object_key, presigned_url).

    The presigned URL is valid for 24 hours and directly accessible by Instagram's
    ingest servers without any additional authentication headers.
    """
    bucket = os.environ["R2_BUCKET"]
    key = f"videos/{run_date}/final.mp4"

    client = _get_client()

    file_size_mb = video_path.stat().st_size / (1024 * 1024)
    logger.info(f"Uploading {video_path.name} ({file_size_mb:.1f} MB) to R2 key: {key}")

    with open(video_path, "rb") as fh:
        client.upload_fileobj(
            fh,
            bucket,
            key,
            ExtraArgs={"ContentType": "video/mp4"},
        )

    presigned_url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=_PRESIGN_EXPIRES,
    )

    logger.success(f"R2 upload complete. Presigned URL valid for 24h.")
    return key, presigned_url


def delete_video(key: str) -> None:
    """Delete an uploaded video from R2 after Instagram confirms publication."""
    bucket = os.environ["R2_BUCKET"]
    client = _get_client()
    client.delete_object(Bucket=bucket, Key=key)
    logger.info(f"R2 object deleted: {key}")
