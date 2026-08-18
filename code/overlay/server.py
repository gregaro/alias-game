"""
server.py — tiny overlay server for the quiz show.

It does two things:
  GET /          -> serves overlay.html (what you add to OBS as a Browser Source)
  GET /state     -> returns the current state.json (the overlay polls this)
  GET /music/... -> serves locally licensed pre-show music

state.json is the single source of truth. For Phase 3 you edit it by hand to
test. In Phase 4 your chat scorer will write to this same file, and the overlay
updates automatically with no changes here.

Convenience for testing the countdown without doing epoch-time math by hand:
  GET /timer/<seconds>  -> stamps "window_ends_at" so the countdown starts now.
  GET /timer/stop       -> clears the countdown.
  GET /start/<minutes>  -> countdown for that many minutes (e.g. /start/5).
  GET /start/<time>     -> countdown to the next local time (e.g. /start/7pm
                           or /start/19:00).
  GET /start/stop       -> clears it, back to idle.
  GET /end              -> after-show end card (leaderboard to center, confetti).
  GET /end/stop         -> clears it, back to idle.

Run:
    pip install flask
    python server.py
Then open http://127.0.0.1:8080 in a browser to preview, and point OBS at the
same URL.
"""

import json
import math
import time
import os
import re
from datetime import datetime, timedelta
from flask import Flask, Response, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
MUSIC_DIR = os.path.join(HERE, "..", "..", "assets", "music")
PORT = 8080

app = Flask(__name__)


def read_state():
    """Read state.json fresh on every request. Returns a safe default if the
    file is missing or mid-write (so the overlay never crashes on a bad read)."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"phase": "idle", "question": None, "window_ends_at": None,
                "starts_at": None, "warmup_messages": [], "leaderboard": []}


def write_state(state):
    """Write state.json atomically (write temp, then replace) so a reader never
    catches a half-written file."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


@app.route("/")
def index():
    return send_from_directory(HERE, "overlay.html")


@app.route("/music/<path:filename>")
def music(filename):
    return send_from_directory(MUSIC_DIR, filename)


@app.route("/state")
def state():
    body = json.dumps(read_state(), ensure_ascii=False)
    resp = Response(body, mimetype="application/json")
    # Never let the browser or OBS cache the state.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/start/<value>")
def start_countdown(value):
    """Pre-show 'starting soon' card: a full-screen countdown to fill the wait
    before the host video rolls. GET /start/<minutes> to count down by duration,
    or GET /start/<time> to end at the next occurrence of a local clock time.
    GET /start/stop clears it.

    A CONTROL endpoint, not a page — hit it from a browser tab once you go
    live on YouTube (scheduled, chat open, nothing rolling yet). Never add it
    as its own OBS Browser Source."""
    s = read_state()
    if value == "stop":
        s["phase"] = "idle"
        s["starts_at"] = None
    else:
        clock = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)", value.lower())
        clock_24 = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
        if clock or clock_24:
            match = clock or clock_24
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
            if clock:
                if not 1 <= hour <= 12 or minute > 59:
                    return Response('{"error":"time must look like 7pm, 7:00pm, or 19:00"}',
                                    status=400, mimetype="application/json")
                hour = hour % 12 + (12 if clock.group(3) == "pm" else 0)
            elif hour > 23 or minute > 59:
                return Response('{"error":"time must look like 7pm, 7:00pm, or 19:00"}',
                                status=400, mimetype="application/json")

            now = datetime.now().astimezone()
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            starts_at = target.timestamp()
        else:
            try:
                minutes = float(value)
            except ValueError:
                return Response('{"error":"use minutes, a time like 7pm or 19:00, or \'stop\'"}',
                                status=400, mimetype="application/json")
            if not math.isfinite(minutes) or minutes < 0:
                return Response('{"error":"minutes must be a finite, non-negative number"}',
                                status=400, mimetype="application/json")
            starts_at = time.time() + minutes * 60
        s["phase"] = "starting"
        s["starts_at"] = starts_at
    # Clear any stale question/window from a previous session — the pre-show
    # card should never bleed a leftover answer window into its own state.
    s["question"] = None
    s["window_ends_at"] = None
    s["warmup_messages"] = []
    write_state(s)
    return Response(json.dumps(s, ensure_ascii=False), mimetype="application/json")


@app.route("/end")
def end_show():
    """Trigger the after-show end card: the leaderboard glides to center stage
    with confetti and a thank-you note, instead of the stream cutting to a
    black frame once the host video finishes.

    A CONTROL endpoint like /timer, not a page — hit it from a browser tab (or
    the scorer) once the outro is winding down. Never add it as its own OBS
    Browser Source."""
    s = read_state()
    s["phase"] = "ended"
    s["question"] = None
    s["window_ends_at"] = None
    write_state(s)
    return Response(json.dumps(s, ensure_ascii=False), mimetype="application/json")


@app.route("/end/stop")
def end_stop():
    """Back to idle — for testing, or if you triggered /end too early."""
    s = read_state()
    s["phase"] = "idle"
    write_state(s)
    return Response(json.dumps(s, ensure_ascii=False), mimetype="application/json")


@app.route("/timer/<value>")
def timer(value):
    """Start (or stop) the answer-window countdown for testing."""
    s = read_state()
    if value == "stop":
        s["window_ends_at"] = None
    else:
        try:
            seconds = int(value)
        except ValueError:
            return Response('{"error":"seconds must be an integer or \'stop\'"}',
                            status=400, mimetype="application/json")
        s["window_ends_at"] = time.time() + seconds
    write_state(s)
    return Response(json.dumps(s, ensure_ascii=False), mimetype="application/json")


if __name__ == "__main__":
    # Bind to all interfaces by default so other devices on your LAN (your
    # laptop running OBS) can reach it. Override with HOST / PORT env vars,
    # e.g. HOST=127.0.0.1 python server.py to keep it local-only.
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", PORT))
    shown = "127.0.0.1" if host in ("0.0.0.0", "127.0.0.1") else host
    print(f"Serving on http://{host}:{port}")
    print(f"  - On this machine:    http://127.0.0.1:{port}")
    print(f"  - From other devices: http://<this-machine-LAN-IP>:{port}")
    print(f"Reading state from {STATE_FILE} (must exist in this folder).")
    app.run(host=host, port=port, threaded=True)
