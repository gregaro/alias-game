"""run_show.py — start the whole live-show stack with one command.

    .venv/bin/python code/run_show.py [--episode ep6]

Starts the overlay server and (if OBS_HOST is set) obs_sync, runs preflight,
waits for your YouTube broadcast to go LIVE, then starts the scorer. All output
is one stream, prefixed by process. Ctrl+C stops everything.

Still three processes, on purpose: the overlay must outlive the scorer (the
countdown runs before it, the end card after it), and a crash in one must not
blank the stream. This only saves you the typing and the ordering.

Policy: server and obs_sync are restarted if they die. The scorer is NOT: a
restart mid-show would re-arm and wait for a sync that already happened.
"""
import argparse
import os
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
LIVE_POLL_SECONDS = 15

sys.path.insert(0, HERE)   # code/, for episode.py
import episode as _episode  # noqa: E402

procs = {}
stopping = threading.Event()


def say(tag, line):
    print(f"[{tag:<7}] {line}", flush=True)


def spawn(tag, args):
    # -u: unbuffered, or a pipe holds the output back until the process exits.
    p = subprocess.Popen([PY, "-u", *args], cwd=ROOT, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    procs[tag] = p
    threading.Thread(target=lambda: [say(tag, l.rstrip()) for l in p.stdout],
                     daemon=True).start()
    return p


def keep_alive(tag, args):
    """Restart on death until we're told to stop."""
    while not stopping.is_set():
        p = spawn(tag, args)
        p.wait()
        if stopping.is_set():
            return
        say(tag, f"exited ({p.returncode}) — restarting in 3s")
        stopping.wait(3)


def wait_for_live():
    """Block until the broadcast is ACTIVE, so the scorer doesn't exit on start."""
    sys.path.insert(0, os.path.join(HERE, "scorer"))
    import chat_scorer
    from youtube_auth import get_service
    service = get_service()
    shown = False
    while not stopping.is_set():
        try:
            if chat_scorer.find_live_chat_id(service):
                return True
        except Exception as e:
            say("launch", f"broadcast lookup failed ({type(e).__name__}) — retrying")
        if not shown:
            say("launch", f"waiting for the broadcast to go LIVE (checking every "
                          f"{LIVE_POLL_SECONDS}s) — go live in YouTube Studio")
            shown = True
        stopping.wait(LIVE_POLL_SECONDS)
    return False


def stop_all(*_):
    stopping.set()
    for p in procs.values():
        if p.poll() is None:
            p.send_signal(signal.SIGINT)    # scorer writes its log on SIGINT


def main():
    ap = argparse.ArgumentParser(description="Start the live-show stack.")
    _episode.add_argument(ap)
    args = ap.parse_args()
    ep_args = ["--episode", args.episode] if args.episode else []

    signal.signal(signal.SIGINT, stop_all)
    signal.signal(signal.SIGTERM, stop_all)

    threading.Thread(target=keep_alive, daemon=True,
                     args=("overlay", ["code/overlay/server.py"])).start()
    if os.environ.get("OBS_HOST") or _env_has_obs():
        threading.Thread(target=keep_alive, daemon=True,
                         args=("obs", ["code/scorer/obs_sync.py"])).start()
    time.sleep(2)       # let the server bind before preflight probes it

    r = subprocess.run([PY, "-u", "code/scorer/preflight.py", *ep_args],
                       cwd=ROOT, capture_output=True, text=True)
    for line in (r.stdout + r.stderr).splitlines():
        say("preflight", line)
    if r.returncode:
        say("launch", "preflight FAILED — fix it and re-run, or Ctrl+C. "
                      "Continuing anyway so the overlay is up for OBS.")

    if wait_for_live():
        say("launch", "broadcast is LIVE — starting scorer")
        sc = spawn("scorer", ["code/scorer/chat_scorer.py", *ep_args])
        sc.wait()
        say("launch", f"scorer finished ({sc.returncode}). Overlay stays up; "
                      "Ctrl+C when you're done.")
    while not stopping.is_set():
        stopping.wait(1)
    for p in procs.values():
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


def _env_has_obs():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
    return bool(os.environ.get("OBS_HOST") and os.environ.get("OBS_SOURCE"))


if __name__ == "__main__":
    main()
