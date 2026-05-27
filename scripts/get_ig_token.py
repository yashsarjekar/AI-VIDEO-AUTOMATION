"""One-time script: exchange a short-lived Facebook token for a long-lived one.

Run locally once (and re-run when the token expires):
  python scripts/get_ig_token.py

Prerequisites:
  1. Create a Facebook Developer App with instagram_basic and
     instagram_content_publish permissions.
  2. Generate a short-lived User Access Token in the Graph API Explorer
     (https://developers.facebook.com/tools/explorer/).
  3. Paste it below when prompted.

The printed IG_ACCESS_TOKEN is valid for ~60 days. Set it as a GitHub secret.
The daily.yml workflow will auto-refresh it when < 15 days remain.
"""

import sys

import requests


_GRAPH_BASE = "https://graph.facebook.com/v19.0"


def main() -> None:
    print("Instagram Long-Lived Token Generator")
    print("=" * 40)
    app_id = input("Enter FB_APP_ID: ").strip()
    app_secret = input("Enter FB_APP_SECRET: ").strip()
    short_token = input("Enter short-lived User Access Token: ").strip()

    # Exchange for a long-lived token (~60 days)
    resp = requests.get(
        f"{_GRAPH_BASE}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_token,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"\nERROR: {resp.status_code} {resp.text}")
        sys.exit(1)

    data = resp.json()
    long_token = data.get("access_token")
    expires_in = data.get("expires_in", 0)

    # Get the IG User ID connected to this token
    me_resp = requests.get(
        f"{_GRAPH_BASE}/me",
        params={"fields": "id,name", "access_token": long_token},
        timeout=10,
    )
    me = me_resp.json() if me_resp.status_code == 200 else {}

    # Find the connected Instagram Business account
    accounts_resp = requests.get(
        f"{_GRAPH_BASE}/me/accounts",
        params={"access_token": long_token},
        timeout=10,
    )
    ig_user_id = None
    if accounts_resp.status_code == 200:
        for page in accounts_resp.json().get("data", []):
            ig_resp = requests.get(
                f"{_GRAPH_BASE}/{page['id']}",
                params={
                    "fields": "instagram_business_account",
                    "access_token": long_token,
                },
                timeout=10,
            )
            ig_data = ig_resp.json().get("instagram_business_account", {})
            if ig_data.get("id"):
                ig_user_id = ig_data["id"]
                break

    print("\n" + "=" * 60)
    print("SUCCESS — add the following to your GitHub repository secrets:")
    print("=" * 60)
    print(f"\nIG_ACCESS_TOKEN={long_token}")
    print(f"IG_USER_ID={ig_user_id or '*** Run get_ig_token.py again after granting page access ***'}")
    print(f"\nToken expires in {expires_in // 86400} days.")
    print("Re-run this script (or let the workflow auto-refresh) before expiry.\n")


if __name__ == "__main__":
    main()
