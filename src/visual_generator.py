"""Stage 3 — Image generation via Pollinations.ai (primary) and Pexels (fallback)."""

from __future__ import annotations

import os
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

import requests
from loguru import logger
from PIL import Image, ImageEnhance, ImageOps

from .schemas import ScriptOutput

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TARGET_W, TARGET_H = 1080, 1920
MIN_FILE_BYTES = 8_000        # smaller than this → treat as placeholder
MIN_DIM_PX = 200              # either dimension below this → treat as placeholder
POLLINATIONS_TIMEOUT = 45     # seconds; Flux can be slow on cold start
PEXELS_TIMEOUT = 15
RETRY_DELAYS = [2, 5]         # seconds between Pollinations retries

POLLINATIONS_URL = (
    "https://image.pollinations.ai/prompt/{prompt}"
    "?width={w}&height={h}&model=flux&nologo=true&enhance=true&seed={seed}"
)
PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"

# ---------------------------------------------------------------------------
# Color grading — applied identically regardless of image source
# ---------------------------------------------------------------------------

def _color_grade(img: Image.Image) -> Image.Image:
    """Slight contrast boost + warm tone shift for visual cohesion."""
    img = img.convert("RGB")

    # Contrast +15%
    img = ImageEnhance.Contrast(img).enhance(1.15)

    # Warmth: split channels, nudge R up, B down
    r, g, b = img.split()

    def _shift(channel: Image.Image, delta: int) -> Image.Image:
        return channel.point(lambda p: max(0, min(255, p + delta)))

    r = _shift(r, +12)
    b = _shift(b, -8)
    img = Image.merge("RGB", (r, g, b))

    # Slight saturation boost to compensate for the warmth desaturation
    img = ImageEnhance.Color(img).enhance(1.10)

    return img


def _resize_to_target(img: Image.Image) -> Image.Image:
    """Crop-fill to exactly 1080×1920 without distortion."""
    return ImageOps.fit(img, (TARGET_W, TARGET_H), method=Image.LANCZOS)


# ---------------------------------------------------------------------------
# Pollinations.ai
# ---------------------------------------------------------------------------

def _fetch_pollinations(prompt: str, scene_index: int) -> Image.Image | None:
    encoded = quote(prompt, safe="")
    # Use scene index as seed for reproducibility on re-runs
    url = POLLINATIONS_URL.format(prompt=encoded, w=TARGET_W, h=TARGET_H, seed=scene_index * 42)

    for attempt, delay in enumerate([0] + RETRY_DELAYS, start=1):
        if delay:
            time.sleep(delay)
        try:
            logger.debug(f"Pollinations attempt {attempt} for scene {scene_index:02d}")
            resp = requests.get(url, timeout=POLLINATIONS_TIMEOUT, stream=True)

            if resp.status_code != 200:
                logger.warning(
                    f"Pollinations returned HTTP {resp.status_code} for scene {scene_index:02d}"
                )
                continue

            content_type = resp.headers.get("Content-Type", "")
            if not content_type.startswith("image/"):
                logger.warning(
                    f"Pollinations non-image Content-Type '{content_type}' for scene {scene_index:02d}"
                )
                continue

            data = resp.content
            if len(data) < MIN_FILE_BYTES:
                logger.warning(
                    f"Pollinations response too small ({len(data)} bytes) — likely placeholder"
                )
                continue

            img = Image.open(BytesIO(data))
            if img.width < MIN_DIM_PX or img.height < MIN_DIM_PX:
                logger.warning(
                    f"Pollinations image too small ({img.width}×{img.height}) for scene {scene_index:02d}"
                )
                continue

            logger.info(
                f"Pollinations OK scene {scene_index:02d} "
                f"({img.width}×{img.height}, {len(data)//1024} KB)"
            )
            return img

        except requests.exceptions.Timeout:
            logger.warning(f"Pollinations timeout on attempt {attempt} for scene {scene_index:02d}")
        except Exception as exc:
            logger.warning(f"Pollinations error on attempt {attempt}: {exc}")

    return None


# ---------------------------------------------------------------------------
# Pexels fallback
# ---------------------------------------------------------------------------

def _pexels_search_query(visual_prompt: str) -> str:
    """Extract a clean short query from the visual prompt for Pexels search."""
    # Take first 4 non-trivial words
    stopwords = {
        "a", "an", "the", "of", "in", "on", "at", "to", "with", "and",
        "or", "is", "are", "was", "were", "be", "been", "being",
    }
    words = [
        w.strip(".,;:!?\"'")
        for w in visual_prompt.split()
        if w.strip(".,;:!?\"'").lower() not in stopwords
    ]
    return " ".join(words[:4])


def _fetch_pexels(visual_prompt: str, scene_index: int) -> Image.Image | None:
    api_key = os.environ.get("PEXELS_API_KEY", "")
    if not api_key:
        logger.warning("PEXELS_API_KEY not set — cannot use Pexels fallback")
        return None

    query = _pexels_search_query(visual_prompt)
    logger.info(f"Pexels fallback for scene {scene_index:02d}, query='{query}'")

    try:
        resp = requests.get(
            PEXELS_SEARCH_URL,
            headers={"Authorization": api_key},
            params={
                "query": query,
                "orientation": "portrait",
                "size": "large",
                "per_page": 5,
            },
            timeout=PEXELS_TIMEOUT,
        )
        resp.raise_for_status()
        photos = resp.json().get("photos", [])
        if not photos:
            logger.warning(f"Pexels returned 0 photos for query '{query}'")
            return None

        # Pick the first result; prefer the largest portrait src available
        photo = photos[0]
        src = photo.get("src", {})
        img_url = src.get("portrait") or src.get("large") or src.get("original")
        if not img_url:
            logger.warning("Pexels photo has no usable src URL")
            return None

        img_resp = requests.get(img_url, timeout=PEXELS_TIMEOUT)
        img_resp.raise_for_status()

        img = Image.open(BytesIO(img_resp.content))
        logger.info(
            f"Pexels OK scene {scene_index:02d} "
            f"({img.width}×{img.height}) via '{query}'"
        )
        return img

    except Exception as exc:
        logger.warning(f"Pexels fallback failed for scene {scene_index:02d}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_visuals(
    run_date: str | None = None,
    script: ScriptOutput | None = None,
) -> list[Path]:
    """Fetch, grade, and save images for all scenes. Returns list of saved paths.

    Idempotent: if all expected scene files already exist, skips generation.
    """
    if run_date is None:
        run_date = datetime.utcnow().strftime("%Y-%m-%d")

    run_dir = Path(__file__).parent.parent / "runs" / run_date

    # Load script if not provided
    if script is None:
        script_path = run_dir / "script.json"
        if not script_path.exists():
            raise FileNotFoundError(
                f"script.json not found for {run_date}. Run script_writer first."
            )
        script = ScriptOutput.model_validate_json(script_path.read_text())

    scene_count = len(script.scenes)
    expected_paths = [run_dir / f"scene_{i:02d}.jpg" for i in range(1, scene_count + 1)]

    # Idempotency: skip if all files present
    if all(p.exists() for p in expected_paths):
        logger.info(f"All {scene_count} scene images already exist for {run_date}, skipping.")
        return expected_paths

    run_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[Path] = []
    failures: list[int] = []

    for i, scene in enumerate(script.scenes, start=1):
        out_path = run_dir / f"scene_{i:02d}.jpg"

        # Idempotency at individual scene level
        if out_path.exists():
            logger.info(f"Scene {i:02d} image already exists, skipping.")
            saved_paths.append(out_path)
            continue

        logger.info(f"Generating image for scene {i:02d}/{scene_count}")

        img: Image.Image | None = _fetch_pollinations(scene.visual_prompt, i)

        if img is None:
            logger.warning(f"Pollinations failed for scene {i:02d}, trying Pexels fallback.")
            img = _fetch_pexels(scene.visual_prompt, i)

        if img is None:
            logger.error(f"Both sources failed for scene {i:02d}. Cannot generate image.")
            failures.append(i)
            continue

        img = _resize_to_target(img)
        img = _color_grade(img)

        img.save(out_path, format="JPEG", quality=90, optimize=True)
        logger.success(f"Scene {i:02d} saved → {out_path.name} ({out_path.stat().st_size // 1024} KB)")
        saved_paths.append(out_path)

    if failures:
        raise RuntimeError(
            f"Failed to generate images for scene(s): {failures}. "
            "Check Pollinations availability and PEXELS_API_KEY."
        )

    return saved_paths
