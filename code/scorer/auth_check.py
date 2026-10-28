"""auth_check.py — is the YouTube token still good, and how old is it?

    .venv/bin/python code/scorer/auth_check.py          # exit 0 ok, 1 broken, 2 ageing

Run daily from cron (see the end of this docstring) so a dead token is found on
a quiet Tuesday, not ten minutes before the show. Also used by preflight.

What this CAN'T do: keep the token alive. While the Google OAuth app is in
"Testing", Google expires the REFRESH token 7 days after it was issued, and
using it (cron or not) does not extend that. The only real fix is publishing
the consent screen ("In production" — an unverified personal app is fine); then
set OAUTH_PUBLISHED=1 in .env to silence the age warning. Until then this gives
you the warning with days to spare, so re-auth is a planned chore, not a crisis.

Token age: Google doesn't report it, so we notice a NEW refresh token (hash kept
in secrets/token_issued.json, never the token itself) and date it from then. The
first ever run can't know, so it is flagged "age unknown" and counts from now.

    0 9 * * *  cd /home/garegin/alias-game && .venv/bin/python code/scorer/auth_check.py
"""
import hashlib
import json
import os
import sys
import time

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, "../../.env"))

import youtube_auth as ya

PRODUCTION_CHANNEL = "Barly Live"
ISSUED_FILE = os.path.join(HERE, "../secrets/token_issued.json")
STATUS_FILE = os.path.join(HERE, "../overlay/auth_status.json")   # -> control page
LOG_FILE = os.path.join(HERE, "../logs/auth_check.log")
TESTING_LIFETIME_DAYS = 7
WARN_AT_DAYS = 5        # leave two days to find a browser and re-auth


def token_age_days(creds):
    """(days, known). Dates a refresh token from the first check that sees it."""
    sha = hashlib.sha256((creds.refresh_token or "").encode()).hexdigest()
    try:
        with open(ISSUED_FILE, "r", encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        rec = {}
    if rec.get("sha") != sha:
        known = bool(rec)       # a previous token existed, so this one is NEW: dated from its file
        try:
            issued = min(os.path.getmtime(ya.TOKEN_FILE), time.time()) if known else time.time()
        except OSError:
            issued = time.time()
        rec = {"sha": sha, "issued_at": issued, "known": known}
        with open(ISSUED_FILE, "w", encoding="utf-8") as f:
            json.dump(rec, f)
    return (time.time() - rec["issued_at"]) / 86400, rec["known"]


def check():
    """Returns dict: ok, message, channel, age_days, age_known, service.
    Never opens a browser flow: a check that hangs on a consent screen is useless."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    out = {"ok": False, "message": "", "channel": None, "age_days": None,
           "age_known": False, "service": None}
    if not os.path.exists(ya.TOKEN_FILE):
        out["message"] = "no token.json — run: .venv/bin/python code/scorer/reauth.py"
        return out
    try:
        creds = Credentials.from_authorized_user_file(ya.TOKEN_FILE, ya.SCOPES)
        if not creds.valid:
            creds.refresh(Request())
            with open(ya.TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        service = ya.get_service(creds)
        me = service.channels().list(part="snippet", mine=True).execute()
        out["channel"] = me["items"][0]["snippet"]["title"]
        out["service"] = service
        out["age_days"], out["age_known"] = token_age_days(creds)
    except Exception as e:      # expired/revoked/network all mean "fix before the show"
        out["message"] = f"{type(e).__name__}: {str(e)[:120]} — run reauth.py"
        return out
    if out["channel"] != PRODUCTION_CHANNEL:
        out["message"] = (f"token controls {out['channel']!r}, expected "
                          f"{PRODUCTION_CHANNEL!r} — re-auth as barlylive@gmail.com")
        return out
    out["ok"] = True
    return out


def age_warning(res):
    """A sentence if the token is nearing its Testing-mode expiry, else None."""
    if os.environ.get("OAUTH_PUBLISHED") == "1" or res["age_days"] is None:
        return None
    if res["age_days"] >= WARN_AT_DAYS:
        return (f"token is {'~' if not res['age_known'] else ''}{res['age_days']:.1f} days old; "
                f"a Testing-mode app expires it at {TESTING_LIFETIME_DAYS} — re-auth soon, or "
                "publish the consent screen and set OAUTH_PUBLISHED=1")
    return None


def main():
    res = check()
    warning = age_warning(res) if res["ok"] else None
    if res["ok"]:
        age = ("age unknown (counting from now)" if not res["age_known"]
               else f"{res['age_days']:.1f} days old")
        msg = f"authorized as {res['channel']}, token {age}"
    else:
        msg = res["message"]
    status = {"ok": res["ok"], "message": msg, "warning": warning,
              "age_days": res["age_days"], "age_known": res["age_known"],
              "checked_at": time.time()}
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(status, f)
    os.replace(tmp, STATUS_FILE)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {'OK  ' if res['ok'] else 'FAIL'}  "
                f"{msg}{'  WARN: ' + warning if warning else ''}\n")
    print(("ok    " if res["ok"] else "FAIL  ") + msg)
    if warning:
        print("WARN  " + warning)
    sys.exit(0 if res["ok"] and not warning else 1 if not res["ok"] else 2)


if __name__ == "__main__":
    main()
