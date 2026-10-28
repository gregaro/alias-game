"""
reauth.py — (re)authorize YouTube OAuth on the headless Pi. No listener.

Why no listener: run_local_server() dies on VS Code's port probe, and an ssh
forward can squat the port. The browser only has to FAIL to reach the redirect
port — the auth code is in the URL either way. So: open the printed URL on the
Mac/phone (Safari is known to work), approve, then paste the failed
http://localhost:53682/?...code=... address back here.

    python code/scorer/reauth.py

Writes ../secrets/token.json. If a stale one exists it is moved aside first.
If Google shows an "already has some access" page that hangs, revoke the app at
myaccount.google.com/permissions and retry (no include_granted_scopes here).

IMPORTANT for agents: start this command with outbound network access already
approved and never stop it while waiting for the callback. OAuth state and the
PKCE verifier are held by this exact process; a callback from one run cannot be
redeemed by another. For production, choose barlylive@gmail.com and afterward
run youtube_auth.py to confirm "Authorized as: Barly Live".
"""

import os
import time
from urllib.parse import urlparse, parse_qs

from google_auth_oauthlib.flow import Flow

from youtube_auth import SCOPES, CLIENT_SECRET_FILE, TOKEN_FILE

# Nothing listens here, on purpose. A stale ssh/VS Code forward on the Mac that
# DOES accept connections on this port makes the browser hang instead of fail;
# pick another with e.g. `REAUTH_PORT=59123 python code/scorer/reauth.py`.
REDIRECT = f"http://localhost:{os.environ.get('REAUTH_PORT', '53682')}/"


def main():
    flow = Flow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES, redirect_uri=REDIRECT)
    url, state = flow.authorization_url(access_type="offline", prompt="consent")

    print("\nOpen this in Safari (recommended) and approve:\n")
    print(url)
    print("\nFor production, sign in explicitly as barlylive@gmail.com.")
    print("Keep THIS process running; its callback cannot be used by another run.")
    print("\nThe next page will fail to load (expected). Copy its FULL address")
    print("from the address bar and paste it below.\n")
    pasted = input("Pasted URL: ").strip()

    q = parse_qs(urlparse(pasted).query)
    if q.get("state", [None])[0] != state:
        raise SystemExit("state mismatch — paste the URL from THIS run's link")
    if "code" not in q:
        raise SystemExit(f"no code in URL (error={q.get('error')})")

    flow.fetch_token(code=q["code"][0])

    if os.path.exists(TOKEN_FILE):
        os.replace(TOKEN_FILE, f"{TOKEN_FILE}.old-{int(time.time())}")
    with open(TOKEN_FILE, "w") as f:
        f.write(flow.credentials.to_json())
    print("Saved", os.path.abspath(TOKEN_FILE))


if __name__ == "__main__":
    main()
