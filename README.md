# Daily Facts & Trivia Video Bot

Automatically generates and publishes one short-form facts/trivia video per day to **YouTube Shorts** and **Instagram Reels**. Runs on a GitHub Actions cron schedule with zero manual intervention after initial setup.

---

## How It Works

```
Claude Haiku          → topic + script
ElevenLabs Flash v2.5 → voiceover (per scene)
Pollinations.ai / Pexels → scene images
faster-whisper        → word-level captions
ffmpeg                → Ken Burns + ASS subtitles + music → final.mp4
YouTube Data API v3   → upload to Shorts
Instagram Graph API   → upload to Reels
SQLite (state.db)     → topic history + upload log (committed back to repo)
```

---

## Prerequisites

| Tool | Where |
|------|-------|
| GitHub account with Actions enabled | — |
| Anthropic account | console.anthropic.com |
| ElevenLabs account (Creator or higher) | elevenlabs.io |
| Google Cloud account | console.cloud.google.com |
| Meta Developer account | developers.facebook.com |
| Cloudflare account (free tier) | cloudflare.com |
| Telegram account | telegram.org |
| Pexels account (free) | pexels.com/api |

---

## Setup Guide

### 1. Inter Bold Font

Download **Inter Bold** (SIL OFL license) from [rsms.me/inter](https://rsms.me/inter/) and save it to:

```
assets/fonts/Inter-Bold.ttf
```

### 2. ElevenLabs — Voice ID

1. Log in at [elevenlabs.io](https://elevenlabs.io).
2. Go to **VoiceLab → Voice Library** or use one of your cloned voices.
3. Click any voice → **ID** tab → copy the UUID (looks like `21m00Tcm4TlvDq8ikWAM`).
4. Open `config.yaml` and set:
   ```yaml
   tts:
     voice_id: "YOUR_VOICE_ID_HERE"
   ```
5. Test it in the playground at **elevenlabs.io/speech-synthesis** before committing.

### 3. Google Cloud Console — YouTube OAuth2

1. Go to [console.cloud.google.com](https://console.cloud.google.com).
2. Create a new project (or use an existing one).
3. **APIs & Services → Enable APIs → YouTube Data API v3** → Enable.
4. **APIs & Services → Credentials → Create Credentials → OAuth Client ID**.
   - Application type: **Desktop App**
   - Download the JSON file → save as `scripts/client_secrets.json` (**never commit this**).
5. **OAuth consent screen** → add your Google account as a test user.
6. Run the one-time token script locally:
   ```bash
   pip install google-auth-oauthlib
   python scripts/get_youtube_token.py
   ```
   A browser window will open. Authorise access. Copy the printed values into GitHub Secrets (see §6).

### 4. Meta Developer App — Instagram Graph API

1. Go to [developers.facebook.com](https://developers.facebook.com) → **Create App → Business**.
2. Add the **Instagram** product.
3. Under **Instagram → Permissions**, request:
   - `instagram_basic`
   - `instagram_content_publish`
   - `pages_read_engagement`
4. Link your Instagram **Business or Creator** account to a Facebook Page.
5. In [Graph API Explorer](https://developers.facebook.com/tools/explorer/):
   - Select your app
   - Generate a **User Access Token** with the permissions above
   - Copy it
6. Exchange for a long-lived token (~60 days) and find your IG User ID:
   ```bash
   python scripts/get_ig_token.py
   ```
   Copy the printed `IG_ACCESS_TOKEN` and `IG_USER_ID` values.

> **Token expiry:** Tokens last 60 days. The workflow auto-refreshes them when < 15 days remain, provided the `GH_PAT` secret is set (see §6).

### 5. Cloudflare R2 — Temporary Video Hosting

Instagram requires a publicly accessible video URL during upload. We use R2 (free: 10 GB storage, 1M Class A ops/month).

1. Log in to [cloudflare.com](https://cloudflare.com) → **R2 Object Storage → Create bucket**.
2. Bucket name: choose anything (e.g. `video-bot-staging`).
3. **Settings → Public access** — enable "Allow public access" (required for presigned URLs to work).
4. **Manage R2 API Tokens → Create API token**:
   - Permissions: **Object Read & Write** on your bucket
   - Copy `Access Key ID` and `Secret Access Key`
5. Your Account ID is in the R2 dashboard URL.

### 6. Telegram Bot — Failure Notifications

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot`.
2. Follow the prompts → copy the **bot token** (looks like `123456:ABC-...`).
3. Start a chat with your new bot (send any message).
4. Get your chat ID:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
   ```
   Look for `"chat": {"id": 123456789}` in the response.

### 7. Background Music

Add royalty-free MP3 tracks to `assets/music/`. Good sources:

- [Pixabay Music](https://pixabay.com/music/) (free, no attribution required)
- [freemusicarchive.org](https://freemusicarchive.org) (check individual licenses)

The pipeline picks one at random per day (seeded by date for reproducibility).

---

## GitHub Secrets Setup

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**.

Add every key from `.env.example`. Additionally:

| Secret | Purpose |
|--------|---------|
| `GH_PAT` | Personal Access Token with `repo` scope — needed for auto-rotating IG and YouTube tokens. Create at github.com/settings/tokens. |

> Without `GH_PAT`, the pipeline still works but token rotation is skipped (with a warning). You'll need to manually refresh tokens before they expire.

---

## First-Run Checklist

- [ ] `assets/fonts/Inter-Bold.ttf` is present
- [ ] `assets/music/` has at least one `.mp3` file
- [ ] `config.yaml` has a real `tts.voice_id`
- [ ] All secrets are set in GitHub (run `gh secret list` to verify names)
- [ ] YouTube channel has at least one public video (avoids API quota issues on new channels)
- [ ] Instagram account is a **Business or Creator** account linked to a Facebook Page
- [ ] Cloudflare R2 bucket public access is enabled
- [ ] Telegram bot has been messaged at least once (to open the chat)
- [ ] Trigger a manual run: **Actions → Daily Video Run → Run workflow**
- [ ] Monitor the Actions log for errors
- [ ] Check the Telegram bot for the success or failure notification

---

## Cost Breakdown

Costs per daily run (estimates — actual depends on script length):

| Service | What's billed | Est. / run | Est. / month |
|---------|--------------|-----------|-------------|
| Anthropic Claude Haiku | ~4 k input + 2 k output tokens | $0.004 | $0.12 |
| ElevenLabs Flash v2.5 | ~600 characters (9 segments) | $0.18 | $5.40 |
| Pollinations.ai | 6–8 images | **Free** | **$0** |
| Pexels API | Fallback only | **Free** | **$0** |
| Cloudflare R2 | ~80 MB upload + delete | **Free tier** | **$0** |
| **Total** | | **~$0.18** | **~$5.50** |

> The pipeline sends a Telegram alert if a single run exceeds **$0.10** (configurable in `config.yaml` under `costs.daily_alert_threshold_usd`).

---

## Troubleshooting

### YouTube token expired / `invalid_grant` error
Re-run `python scripts/get_youtube_token.py` locally and update the `YOUTUBE_REFRESH_TOKEN` secret.

### YouTube quota exceeded (`quotaExceeded`)
The YouTube Data API v3 has a 10,000 unit/day quota. Each upload costs ~1,600 units. If you hit the limit, wait until midnight Pacific Time for the quota to reset. Apply for a quota increase at console.cloud.google.com if needed.

### Instagram `aspect_ratio_not_supported`
The video must be 9:16 ratio (1080×1920). Check `visual_generator.py`'s `_resize_to_target` call — the `ImageOps.fit` should always produce the correct dimensions.

### Instagram `media_type_not_supported` or container stuck in `IN_PROGRESS`
- The video must be H.264, AAC audio, MP4 container.
- The presigned R2 URL must be reachable by Meta's servers (test with `curl -I <url>`).
- Check that R2 bucket public access is enabled.
- Reels must be 3–90 seconds; ours target 45s so this shouldn't trigger.

### faster-whisper model download (~145 MB)
The first run downloads `small.en` model to `~/.cache/huggingface`. The Actions workflow caches this directory under key `whisper-small-en-int8-v1`. Subsequent runs load it from cache in seconds.

### `REPLACE_WITH_VOICE_ID` error
Edit `config.yaml` and set a real ElevenLabs voice ID. See §2 of the setup guide.

### IG token expiry (`OAuthException: Error validating access token`)
The token lasted > 60 days without refresh. Run `python scripts/get_ig_token.py` locally and update `IG_ACCESS_TOKEN`. Then set `GH_PAT` so auto-refresh works going forward.

### Video file > 80 MB
Increase CRF in `video_builder.py` (`crf=26` or higher reduces quality but shrinks file size). Or reduce `scenes_max` in `config.yaml`.

---

## Project Structure

```
youtube-insta-bot/
├── src/
│   ├── main.py              # orchestrator
│   ├── topic_generator.py   # Stage 1
│   ├── script_writer.py     # Stage 2
│   ├── visual_generator.py  # Stage 3
│   ├── voice_generator.py   # Stage 4
│   ├── caption_generator.py # Stage 5
│   ├── video_builder.py     # Stage 6
│   ├── metadata_generator.py# Stage 7
│   ├── uploader.py          # Stage 8
│   ├── analytics.py         # weekly stats
│   ├── storage.py           # Cloudflare R2 helper
│   ├── notifications.py     # Telegram
│   ├── db.py                # SQLite helpers
│   └── schemas.py           # Pydantic models
├── assets/
│   ├── fonts/Inter-Bold.ttf # download separately (SIL OFL)
│   └── music/               # add your own royalty-free tracks
├── tests/
├── scripts/
│   ├── get_youtube_token.py
│   └── get_ig_token.py
├── runs/                    # daily outputs (JSON/logs committed; binaries gitignored)
├── reports/                 # weekly markdown reports
├── .github/workflows/
│   ├── daily.yml
│   └── weekly_analytics.yml
├── config.yaml
├── .env.example
├── requirements.txt
└── state.db                 # committed — topic history + upload log
```
