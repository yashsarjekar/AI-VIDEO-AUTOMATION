"""Quick local test for ElevenLabs voice — run before pushing to CI."""

import os
import sys
from pathlib import Path

# Load .env if present
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

api_key = os.environ.get("ELEVENLABS_API_KEY", "")
if not api_key:
    print("ERROR: ELEVENLABS_API_KEY not set. Add it to .env or export it.")
    sys.exit(1)

# Read voice_id from config.yaml
import yaml
config_path = Path(__file__).parent.parent / "config.yaml"
config = yaml.safe_load(config_path.read_text())
voice_id = config["tts"]["voice_id"]
model = config["tts"]["model"]

print(f"Voice ID : {voice_id}")
print(f"Model    : {model}")
print(f"API Key  : {api_key[:8]}...")
print()

TEST_TEXT = "Octopuses have three hearts, and two of them stop beating every time they swim."

print(f"Generating: \"{TEST_TEXT}\"")

try:
    from elevenlabs.client import ElevenLabs
    from elevenlabs import VoiceSettings

    client = ElevenLabs(api_key=api_key)
    audio_chunks = client.text_to_speech.convert(
        voice_id=voice_id,
        text=TEST_TEXT,
        model_id=model,
        output_format="mp3_44100_128",
        voice_settings=VoiceSettings(
            stability=0.50,
            similarity_boost=0.75,
            style=0.0,
            use_speaker_boost=True,
        ),
    )
    audio_bytes = b"".join(audio_chunks)

    out_path = Path(__file__).parent / "test_voice_output.mp3"
    out_path.write_bytes(audio_bytes)
    print(f"\nSUCCESS — saved to {out_path} ({len(audio_bytes)//1024} KB)")
    print("Play it with:  open scripts/test_voice_output.mp3")

except Exception as exc:
    print(f"\nFAILED — {exc}")
    sys.exit(1)
