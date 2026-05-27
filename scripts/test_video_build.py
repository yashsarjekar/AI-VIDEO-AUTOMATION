"""End-to-end local test for video_builder.
Generates all inputs via ffmpeg/PIL (no pydub needed) then calls _build_ffmpeg_video directly.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

TEST_DATE = "0001-01-02"
RUN_DIR = ROOT / "runs" / TEST_DATE

shutil.rmtree(RUN_DIR, ignore_errors=True)
RUN_DIR.mkdir(parents=True)

# ---------------------------------------------------------------------------
# 1. Create test scene images (1080x1920)
# ---------------------------------------------------------------------------

colours = [(200, 100, 50), (50, 150, 200)]
for i, colour in enumerate(colours, 1):
    Image.new("RGB", (1080, 1920), colour).save(RUN_DIR / f"scene_{i:02d}.jpg")
print("✓ Created test images")

# ---------------------------------------------------------------------------
# 2. Create test voice MP3s via ffmpeg (silent audio, known durations)
# Worst-case: scene_01.jpg used for both hook (2s) and scene 1 (2s) — same image + same duration
# scene_02.jpg used for scene 2 (2s) and CTA (2s) — same image + same duration
# ---------------------------------------------------------------------------

VOICE_DURATIONS = {
    "voice_00.mp3": 2.0,   # hook — uses scene_01.jpg
    "voice_01.mp3": 2.0,   # scene 1 — uses scene_01.jpg, SAME duration as hook
    "voice_02.mp3": 2.0,   # scene 2 — uses scene_02.jpg
    "voice_cta.mp3": 2.0,  # CTA — uses scene_02.jpg, SAME duration as scene 2
}

for name, dur in VOICE_DURATIONS.items():
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono",
        "-t", str(dur), "-q:a", "9", str(RUN_DIR / name)
    ], check=True, capture_output=True)

# Create voice_full.mp3 (concat of all segments)
concat_list = RUN_DIR / "concat.txt"
concat_list.write_text("\n".join(
    f"file '{RUN_DIR / n}'" for n in VOICE_DURATIONS
))
subprocess.run([
    "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
    "-c", "copy", str(RUN_DIR / "voice_full.mp3")
], check=True, capture_output=True)
print("✓ Created voice MP3s via ffmpeg")

# ---------------------------------------------------------------------------
# 3. Create minimal captions.json
# ---------------------------------------------------------------------------

captions_data = {
    "chunks": [
        {"words": [{"word": "Test", "start": 0.0, "end": 0.3}, {"word": "caption", "start": 0.3, "end": 0.7}], "start": 0.0, "end": 0.7},
    ],
    "total_duration": sum(VOICE_DURATIONS.values()),
}
(RUN_DIR / "captions.json").write_text(json.dumps(captions_data))
print("✓ Created captions.json")

# ---------------------------------------------------------------------------
# 4. Build clip schedule manually (bypasses pydub)
# Mirror _get_clip_schedule logic:
#   voice_00 → scene_01.jpg (hook)
#   voice_01 → scene_01.jpg (scene 1)
#   voice_02 → scene_02.jpg (scene 2)
#   voice_cta → scene_02.jpg (CTA)
# Each duration includes 150ms gap except last
# ---------------------------------------------------------------------------

SILENCE_GAP = 0.150
segments = [
    (RUN_DIR / "scene_01.jpg", VOICE_DURATIONS["voice_00.mp3"] + SILENCE_GAP),  # hook + gap
    (RUN_DIR / "scene_01.jpg", VOICE_DURATIONS["voice_01.mp3"] + SILENCE_GAP),  # scene 1 + gap
    (RUN_DIR / "scene_02.jpg", VOICE_DURATIONS["voice_02.mp3"] + SILENCE_GAP),  # scene 2 + gap
    (RUN_DIR / "scene_02.jpg", VOICE_DURATIONS["voice_cta.mp3"]),               # CTA (no gap)
]
total_sec = sum(d for _, d in segments)
print(f"✓ Clip schedule: {len(segments)} clips, {total_sec:.1f}s total")

# ---------------------------------------------------------------------------
# 5. Build ASS captions file
# ---------------------------------------------------------------------------

from src.schemas import CaptionsOutput
from src.video_builder import _build_ass

captions = CaptionsOutput.model_validate(captions_data)
ass_path = RUN_DIR / "captions.ass"
_build_ass(captions, ass_path)
print("✓ Built ASS captions")

# ---------------------------------------------------------------------------
# 6. Pick a music track
# ---------------------------------------------------------------------------

music_dir = ROOT / "assets" / "music"
tracks = list(music_dir.glob("*.mp3")) + list(music_dir.glob("*.wav"))
music_path = tracks[0] if tracks else None
print(f"✓ Music: {music_path.name if music_path else 'none'}")

# ---------------------------------------------------------------------------
# 7. Run _build_ffmpeg_video
# ---------------------------------------------------------------------------

from src.video_builder import _build_ffmpeg_video

output_path = RUN_DIR / "final.mp4"
print(f"\nRunning ffmpeg assembly ({total_sec:.1f}s video)...")
_build_ffmpeg_video(segments, RUN_DIR / "voice_full.mp3", music_path, ass_path, output_path)

# ---------------------------------------------------------------------------
# 8. Verify output
# ---------------------------------------------------------------------------

probe = subprocess.run(
    ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(output_path)],
    capture_output=True, text=True, check=True
)
import json as _json
duration = float(_json.loads(probe.stdout)["format"]["duration"])
size_mb = output_path.stat().st_size / (1024 * 1024)

print(f"\nOutput: {output_path.name}")
print(f"  Duration : {duration:.2f}s  (expected ~{total_sec:.1f}s)")
print(f"  Size     : {size_mb:.2f} MB")

if abs(duration - total_sec) > 2.0:
    print(f"WARN: duration mismatch ({duration:.2f}s vs expected {total_sec:.2f}s)")
else:
    print("✓ Duration looks correct")

shutil.rmtree(RUN_DIR)
print("\nSUCCESS")
