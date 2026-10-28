"""preflight.py — one command that checks everything show night depends on.

    .venv/bin/python code/scorer/preflight.py            # days/hours before
    .venv/bin/python code/scorer/preflight.py --live     # after going live

Why: every one of these used to be discovered mid-setup, with viewers waiting.
Exit code is 1 if anything FAILED, so it can gate a launcher script. WARN means
"playable, but you'll lose something" (e.g. no auto end card).

Without --live, "broadcast not live yet" is only a warning (you're checking
early); with --live it is a failure, because the scorer needs it.
"""
import argparse
import contextlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))   # code/, for episode.py
import episode
import chat_scorer
from normalize import normalize

PRODUCTION_CHANNEL = "Barly Live"
OVERLAY_PORT = int(os.environ.get("PORT", 8080))
AGENT_CONFIG = os.path.join(HERE, "../agents/config.yaml")

failures = 0


def ok(msg):
    print(f"  ok    {msg}")


def warn(msg):
    print(f"  WARN  {msg}")


def fail(msg):
    global failures
    failures += 1
    print(f"  FAIL  {msg}")


def check_episode(name):
    """Returns the episode dir, or None if later checks can't run."""
    try:
        ep = episode.resolve(name)
    except SystemExit as e:
        fail(str(e).splitlines()[0])
        return None
    ok(f"episode: {ep.name}")
    return ep


def check_questions(ep):
    path = ep / episode.QUESTIONS
    try:
        qs = json.loads(path.read_text(encoding="utf-8"))["questions"]
    except (OSError, ValueError, KeyError) as e:
        fail(f"{episode.QUESTIONS} unreadable: {e}")
        return None
    bad, thin = [], []
    for i, q in enumerate(qs, 1):
        answers = [a for a in q.get("answers", []) if normalize(a)]
        if not answers:
            bad.append(f"Q{i} has no answers")
        elif len(answers) < 2:
            # One spelling is legal but usually means the variants pass was skipped.
            thin.append(f"Q{i}")
        if len(q.get("hints") or []) < 2:
            bad.append(f"Q{i} has fewer than 2 hints")
    if thin:
        warn(f"{', '.join(thin)}: only one accepted spelling (variants pass skipped?)")
    for b in bad:
        fail(b)
    if not bad:
        ok(f"{len(qs)} questions, all have answers and hints")
    return qs


def check_timeline(ep, qs):
    chat_scorer.TIMELINE_FILE = str(ep / episode.TIMELINE)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):       # the loader explains itself by printing
        tl = chat_scorer.load_timeline(len(qs))
    notes = [l for l in buf.getvalue().splitlines() if l.strip()]
    if tl is None:
        fail("timeline.json unusable — scorer would fall back to FIXED windows"
             + "".join(f"\n          {n}" for n in notes))
        return None
    for n in notes:
        warn(n)
    ok(f"timeline: {len(tl['starts'])} windows, last closes at {tl['outro_start']:.0f}s")

    # Row words are only a label, but a mismatch means the timeline was measured
    # against a different render than these questions.
    rows = json.loads((ep / episode.TIMELINE).read_text(encoding="utf-8"))["windows"]
    off = [i + 1 for i, (r, q) in enumerate(zip(rows, qs))
           if r.get("word") and normalize(r["word"]) != normalize(q.get("word", ""))]
    if off:
        fail(f"timeline words differ from questions at Q{off} — wrong render?")
    if not tl["hints"]:
        warn("no teaser/hint marks — overlay shows every hint at window open")
    if tl["outro_end"] is None:
        warn("no outro_end — end card must be triggered by hand (GET /end)")
    else:
        ok(f"end card auto-fires at {tl['outro_end']:.0f}s")
    return tl


def check_video(path, tl):
    if not os.path.isfile(path):
        fail(f"video not found: {path}")
        return
    ok(f"video exists: {path}")
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path], capture_output=True, text=True, timeout=20)
        dur = float(out.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        warn("ffprobe unavailable — skipped video duration check")
        return
    last = tl["outro_end"] if tl and tl["outro_end"] is not None else tl["outro_start"] if tl else None
    if last is not None and dur < last:
        fail(f"video is {dur:.0f}s but timeline runs to {last:.0f}s — wrong render?")
    else:
        ok(f"video length {dur:.0f}s covers the timeline")


def check_auth():
    """Returns a YouTube service, or None. Never opens a browser flow: a preflight
    that hangs on a consent screen is worse than one that says 're-auth'."""
    import auth_check
    res = auth_check.check()
    if not res["ok"]:
        fail(f"YouTube auth: {res['message']}")
        return None
    ok(f"authorized as {res['channel']} (token {res['age_days']:.1f} days old"
       f"{'' if res['age_known'] else ', age unknown'})")
    warning = auth_check.age_warning(res)
    if warning:
        warn(warning)
    return res["service"]


def check_broadcast(service, need_live):
    try:
        chat_id = chat_scorer.find_live_chat_id(service)
    except Exception as e:
        fail(f"broadcast lookup failed ({type(e).__name__}: {str(e)[:120]})")
        return
    if chat_id:
        ok("broadcast is LIVE, chat found")
    elif need_live:
        fail("no ACTIVE broadcast — go live (scheduled isn't enough)")
    else:
        warn("no active broadcast yet (fine if you haven't gone live; re-run with --live after)")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))        # no packet sent; just picks the outbound interface
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def check_overlay():
    def get(host):
        with urllib.request.urlopen(f"http://{host}:{OVERLAY_PORT}/state", timeout=3) as r:
            return json.load(r)
    try:
        state = get("127.0.0.1")
    except Exception:
        fail(f"overlay server not answering on :{OVERLAY_PORT} — run: python code/overlay/server.py")
        return
    ok(f"overlay server up (phase: {state.get('phase')})")
    ip = lan_ip()
    if not ip:
        warn("couldn't determine LAN IP")
        return
    try:
        get(ip)
        ok(f"reachable on LAN: http://{ip}:{OVERLAY_PORT}  <- OBS Browser Source URL")
    except Exception:
        fail(f"not reachable at http://{ip}:{OVERLAY_PORT} — bound to 127.0.0.1, or firewall")


def check_obs():
    import obs_sync
    cfg = obs_sync.settings()
    if not cfg:
        warn("OBS sync not configured (OBS_HOST/OBS_SOURCE) — you'll press START by hand")
        return
    good, msg = obs_sync.check(cfg)
    (ok if good else fail)(msg)
    st = None
    try:
        st = json.load(open(os.path.join(HERE, "../overlay/obs_status.json")))
    except (OSError, ValueError):
        pass
    if not st or time.time() - st.get("updated", 0) > 15:
        warn("obs_sync.py isn't running — start it: .venv/bin/python code/scorer/obs_sync.py")


def check_rehearsal_flag():
    try:
        text = open(AGENT_CONFIG, encoding="utf-8").read()
    except OSError:
        return
    if re.search(r"^rehearsal_mode:\s*true", text, re.M):
        warn("rehearsal_mode is ON in agents/config.yaml (only matters for generating words)")


def main():
    p = argparse.ArgumentParser(description="Pre-show checks.")
    episode.add_argument(p)
    p.add_argument("--live", action="store_true",
                   help="you've gone live: a missing active broadcast is a failure")
    p.add_argument("--video", metavar="PATH",
                   help="rendered video file: check it exists and covers the timeline")
    p.add_argument("--offline", action="store_true",
                   help="skip YouTube checks (auth, broadcast)")
    args = p.parse_args()

    print("Preflight")
    ep = check_episode(args.episode)
    if ep:
        qs = check_questions(ep)
        tl = check_timeline(ep, qs) if qs else None
        if args.video:
            check_video(args.video, tl)
    check_overlay()
    if not args.offline:
        service = check_auth()
        if service:
            check_broadcast(service, args.live)
    check_obs()
    check_rehearsal_flag()

    print()
    if failures:
        print(f"NOT READY: {failures} failure(s).")
        sys.exit(1)
    print("READY." if args.live else "READY (re-run with --live once the broadcast is up).")


if __name__ == "__main__":
    main()
