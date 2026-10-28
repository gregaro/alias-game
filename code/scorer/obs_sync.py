"""obs_sync.py — fire the show's start sync from OBS, not from a human.

OBS (on the Mac) announces the instant the avatar Media Source starts playing.
We forward that to the overlay server's POST /api/sync, which hands the armed
scorer its t0 — the same path as the control page's START button, minus the
reaction time.

Run on the Pi, next to the overlay server and scorer:
    .venv/bin/python code/scorer/obs_sync.py

Config (repo-root .env):
    OBS_HOST=192.168.x.x     the Mac's LAN IP
    OBS_PORT=4455            OBS -> Tools -> WebSocket Server Settings
    OBS_PASSWORD=...
    OBS_SOURCE=Avatar        the Media Source's name, EXACTLY as in OBS

It never decides anything: /api/sync refuses unless the scorer is armed, so a
replayed or restarted clip can't re-sync a running show.
"""
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request

import obsws_python as obs
from dotenv import load_dotenv

# The library logs a full traceback on every failed connect. We report failures
# ourselves in one line, and retry every few seconds, so silence it.
logging.getLogger("obsws_python").setLevel(logging.CRITICAL)

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, "../../.env"))

STATUS_FILE = os.path.join(HERE, "../overlay/obs_status.json")   # -> control page
SYNC_URL = f"http://127.0.0.1:{os.environ.get('PORT', 8080)}/api/sync"
POLL_SECONDS = 2
DEBOUNCE_SECONDS = 5    # OBS can emit a start twice (restart + play); one sync is enough


def settings():
    """Connection settings from the environment, or None if OBS isn't configured."""
    host, source = os.environ.get("OBS_HOST"), os.environ.get("OBS_SOURCE")
    if not host or not source:
        return None
    return {"host": host, "port": int(os.environ.get("OBS_PORT", 4455)),
            "password": os.environ.get("OBS_PASSWORD", ""), "source": source}


def check(cfg, timeout=3):
    """(ok, message): OBS reachable and the named source exists. Also used by
    preflight, so a typo in OBS_SOURCE shows up BEFORE the show rather than as a
    silent no-sync."""
    try:
        req = obs.ReqClient(host=cfg["host"], port=cfg["port"],
                            password=cfg["password"], timeout=timeout)
        names = [i["inputName"] for i in req.get_input_list().inputs]
        req.disconnect()
    except Exception as e:
        return False, f"can't reach OBS at {cfg['host']}:{cfg['port']} ({type(e).__name__}: {str(e)[:80]})"
    if cfg["source"] not in names:
        return False, f"OBS has no input named {cfg['source']!r} (has: {', '.join(names) or 'none'})"
    return True, f"OBS connected, source {cfg['source']!r} found"


def write_status(**fields):
    fields["updated"] = time.time()
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(fields, f)
    os.replace(tmp, STATUS_FILE)


def post_sync():
    try:
        req = urllib.request.Request(SYNC_URL, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=3):
            return "SYNCED"
    except urllib.error.HTTPError as e:
        # 409 = scorer not armed. Expected for a rehearsal clip or a replay; say so.
        return f"ignored (HTTP {e.code}: scorer not armed)"
    except OSError as e:
        return f"FAILED to reach overlay server ({e})"


def run(cfg):
    last = {"t": 0.0}

    def on_media_input_playback_started(data):
        if data.input_name != cfg["source"]:
            return
        now = time.time()
        if now - last["t"] < DEBOUNCE_SECONDS:
            return
        last["t"] = now
        print(f"{time.strftime('%H:%M:%S')}  OBS started {data.input_name!r} -> {post_sync()}")

    events = obs.EventClient(host=cfg["host"], port=cfg["port"], password=cfg["password"])
    events.callback.register(on_media_input_playback_started)
    try:
        while True:
            ok, msg = check(cfg)
            write_status(connected=ok, message=msg, source=cfg["source"])
            if not ok:
                raise ConnectionError(msg)
            time.sleep(POLL_SECONDS)
    finally:
        events.disconnect()


def main():
    cfg = settings()
    if not cfg:
        sys.exit("Set OBS_HOST and OBS_SOURCE (and OBS_PORT/OBS_PASSWORD) in .env — see the top of this file.")
    print(f"Watching OBS at {cfg['host']}:{cfg['port']} for {cfg['source']!r} ...")
    shown = None
    while True:         # OBS restarts, the Mac sleeps: keep trying rather than die
        try:
            run(cfg)
        except KeyboardInterrupt:
            write_status(connected=False, message="obs_sync stopped", source=cfg["source"])
            return
        except Exception as e:
            write_status(connected=False, message=str(e)[:160], source=cfg["source"])
            if str(e) != shown:
                print(f"{time.strftime('%H:%M:%S')}  {e} — retrying")
                shown = str(e)
            time.sleep(3)


if __name__ == "__main__":
    main()
