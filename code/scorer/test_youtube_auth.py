# pip install google-auth-oauthlib google-api-python-client
import os
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

HERE = os.path.dirname(os.path.abspath(__file__))
flow = InstalledAppFlow.from_client_secrets_file(os.path.join(HERE, "../secrets/client_secret.json"), SCOPES)
creds = flow.run_local_server(port=0)        # opens your browser to approve

youtube = build("youtube", "v3", credentials=creds)
me = youtube.channels().list(part="snippet", mine=True).execute()
print("Authorized as:", me["items"][0]["snippet"]["title"])
