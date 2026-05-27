"""One-time script: obtain the initial YouTube OAuth2 refresh token.

Run locally ONCE before setting up GitHub Actions:
  python scripts/get_youtube_token.py

Prerequisites:
  1. Enable YouTube Data API v3 in Google Cloud Console.
  2. Create an OAuth 2.0 Client ID (Desktop App type).
  3. Download the JSON file as client_secrets.json in this directory.

The script will open a browser window for you to authorise access.
Copy the printed YOUTUBE_REFRESH_TOKEN value into your GitHub secret.
"""

import json
import sys
from pathlib import Path

SECRETS_FILE = Path(__file__).parent / "client_secrets.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main() -> None:
    if not SECRETS_FILE.exists():
        print(
            f"ERROR: {SECRETS_FILE} not found.\n"
            "Download your OAuth2 client credentials from Google Cloud Console\n"
            "and save them as scripts/client_secrets.json"
        )
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Run: pip install google-auth-oauthlib")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(str(SECRETS_FILE), SCOPES)
    creds = flow.run_local_server(port=8080, open_browser=True)

    print("\n" + "=" * 60)
    print("SUCCESS — add the following to your GitHub repository secrets:")
    print("=" * 60)
    print(f"\nYOUTUBE_REFRESH_TOKEN={creds.refresh_token}")
    print(f"YOUTUBE_CLIENT_ID={creds.client_id}")
    print(f"YOUTUBE_CLIENT_SECRET={creds.client_secret}")
    print("\nNEVER share or commit these values.\n")


if __name__ == "__main__":
    main()
