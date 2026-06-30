"""
youtube_auth.py — reusable YouTube OAuth with token caching.

First run: opens a browser once to authorize.
Later runs: loads the cached token and refreshes it silently — no browser.

Install once:
    pip install google-auth-oauthlib google-api-python-client

Files this expects / creates (keep both out of git):
    client_secret.json  — the OAuth client you downloaded from Google Cloud
    token.json          — created automatically after the first authorization
"""

import os
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# youtube.force-ssl covers reading live chat AND posting to it later.
# If you only ever read, you could narrow this to youtube.readonly.
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRET_FILE = os.path.join(HERE, "../secrets/client_secret.json")
TOKEN_FILE = os.path.join(HERE, "../secrets/token.json")


def get_credentials():
    """Return valid OAuth credentials, using the cache when possible."""
    creds = None

    # 1. Try the cached token.
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    # 2. If it's missing or invalid, refresh or re-authorize.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # Silent refresh — no browser.
            creds.refresh(Request())
        else:
            # First run, or refresh token revoked/expired: browser flow.
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        # Save (or update) the token for next time.
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return creds


def get_service():
    """Return an authorized YouTube Data API v3 service object."""
    return build("youtube", "v3", credentials=get_credentials())


if __name__ == "__main__":
    # Quick self-test: prints your channel name.
    youtube = get_service()
    me = youtube.channels().list(part="snippet", mine=True).execute()
    print("Authorized as:", me["items"][0]["snippet"]["title"])
