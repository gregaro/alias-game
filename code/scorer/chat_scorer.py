"""
chat_scorer.py — read YouTube live chat, score answers, drive the overlay.

Closes the loop: viewers' chat answers move the leaderboard the overlay shows.

How it works
------------
- Authenticates with your existing youtube_auth.get_service().
- Finds your active broadcast's liveChatId.
- Runs a background thread that polls liveChatMessages.list at the interval the
  API tells it to (pollingIntervalMillis).
- You sync the show with ONE keypress: press Enter the moment the video starts.
  From there the scorer opens/closes each question's answer window on its own.
  While a window is open, correct answers score with speed decay (1st correct =
  most points, then less). Scores are cumulative across questions.
- After every scoring change it writes state.json — the SAME file your overlay
  server reads — so names pop onto the leaderboard live.

Two timing modes (chosen automatically):
- TIMELINE mode (preferred): if ../questions/timeline.json exists, each window
  opens at a measured second-mark read off the rendered video. The host reads
  variable-length rounds (the [pause N seconds] tags make each window a
  different length), so fixed windows would drift out of sync. starts[i] is
  when question i's window opens, relative to the video-start sync press;
  window i closes when window i+1 opens (i.e. the instant the host reveals
  answer i), and the last window closes at outro_start. See timeline.json.
- FIXED mode (fallback): no timeline.json → every window is window_seconds
  long, back-to-back. Fine for a dry run before you've measured the video.

This is the Phase 4 / Phase 5 orchestrator in its simplest honest form: you
sync it once, the timeline (or the fixed clock) advances questions. Fully
automating the render+measure step is a backlog item.

Run (with youtube_auth.py, normalize.py, questions.json, state.json alongside):
    pip install google-api-python-client google-auth-oauthlib flask
    python chat_scorer.py
"""

import json
import os
import sys
import time
import threading
from datetime import datetime, timezone

from youtube_auth import get_service
from normalize import matches

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "../overlay/state.json")
QUESTIONS_FILE = os.path.join(HERE, "../questions/questions.json")
TIMELINE_FILE = os.path.join(HERE, "../questions/timeline.json")


# ----------------------------- state file I/O -----------------------------

def write_state(state: dict):
    """Atomically write state.json so the overlay never reads a half file."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def load_questions() -> dict:
    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_mark(v) -> float:
    """Accept a second-mark as a number (95) or a clock string ("1:35",
    "0:07", "1:02:03") and return seconds. Written this way so the marks you
    jot down while scrubbing the video paste straight into timeline.json."""
    if isinstance(v, (int, float)):
        return float(v)
    secs = 0.0
    for part in str(v).strip().split(":"):
        secs = secs * 60 + float(part)
    return secs


def spoken_hints(q) -> list[str]:
    """The hints the host actually SAYS, in the order he says them.

    generate_show_script.py builds each round as teaser=hints[0],
    closer=hints[-1], and hints[1] is the spare it never reads on air. The
    overlay must mirror the voice, so we rebuild that same pair here rather
    than using q["text"] (which joins ALL three hints, spare included).
    Falls back to the joined text if a question has no hints list."""
    hints = q.get("hints") or []
    pair = [h for h in dict.fromkeys([hints[0], hints[-1]]) if h] if hints else []
    return pair or [q["text"]]


def load_timeline(n_questions: int):
    """Return {"starts": [...], "outro_start": float, "hints": [[...], ...] |
    None} measured off the rendered video, or None to fall back to fixed
    windows. None is returned (with a printed reason) whenever the file is
    absent or doesn't cleanly describe every question, so a half-filled
    timeline can never desync the show — better to run fixed than to run wrong.

    timeline.json is one row per word: {"start", "teaser", "hint"}. "start" and
    "outro_start" are the scoring boundaries. "teaser"/"hint" are OPTIONAL and
    only drive the overlay — bad ones degrade to showing every hint at window
    open, so an unusable mark costs us pacing, not points."""
    if not os.path.exists(TIMELINE_FILE):
        return None
    with open(TIMELINE_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)

    rows = raw.get("windows") or []
    outro = raw.get("outro_start")
    if not rows or outro is None:
        print("timeline.json present but not filled in — using fixed windows. "
              "Set each window's 'start' and the 'outro_start'.")
        return None

    if len(rows) != n_questions:
        print(f"timeline.json has {len(rows)} windows but there are "
              f"{n_questions} questions — using fixed windows.")
        return None
    if any(r.get("start") is None for r in rows):
        print("timeline.json is missing a window 'start' — using fixed windows.")
        return None

    starts = [parse_mark(r["start"]) for r in rows]
    bounds = starts + [parse_mark(outro)]
    if any(b <= a for a, b in zip(bounds, bounds[1:])):
        print("timeline.json marks are not strictly increasing "
              "(each window must open after the previous one) — using fixed windows.")
        return None
    return {"starts": starts, "outro_start": bounds[-1],
            "hints": load_hint_marks(rows, bounds)}


def load_hint_marks(rows: list[dict], bounds: list[float]):
    """Validate the optional per-window teaser/hint marks, or return None to
    show every hint at window open. Each mark must land inside its own window:
    a hint that fires before its window opens would be on screen while the host
    is still revealing the previous word, and one that fires after it closes
    would never show at all. All-or-nothing — a half-marked show would reveal
    hints on some words and not others, which just looks broken on stream."""
    out = []
    for i, row in enumerate(rows):
        marks = [row.get("teaser"), row.get("hint")]
        if all(m is None for m in marks):
            if out:
                print(f"timeline.json window {i + 1} has no teaser/hint marks "
                      "while others do — showing all hints at window open.")
                return None
            return None
        if any(m is None for m in marks):
            print(f"timeline.json window {i + 1} has only one of teaser/hint — "
                  "showing all hints at window open.")
            return None

        marks = [parse_mark(m) for m in marks]
        if marks[1] <= marks[0]:
            print(f"timeline.json window {i + 1}: 'hint' must come after "
                  "'teaser' — showing all hints at window open.")
            return None
        if not (bounds[i] <= marks[0] and marks[1] < bounds[i + 1]):
            print(f"timeline.json window {i + 1}: teaser/hint fall outside the "
                  f"window ({bounds[i]:.0f}s–{bounds[i + 1]:.0f}s) — "
                  "showing all hints at window open.")
            return None
        out.append(marks)

    # A window opens on the host revealing the PREVIOUS word, so the teaser
    # always lands a few seconds later. Teasers sitting on top of their starts
    # means the 'start' marks are really teaser marks — which would leave each
    # reveal INSIDE the previous window, letting chat score by copying the host.
    # Not fatal (scoring still runs), but it is almost certainly a mis-measure.
    if sum(t - bounds[i] < 1.0 for i, (t, _) in enumerate(out)) > 1:
        print("WARNING: teaser marks sit on their window 'start' marks. A window "
              "should open on the host revealing the PREVIOUS answer, seconds "
              "BEFORE this word's teaser. If 'start' is really where the teaser "
              "begins, every answer is being revealed while its window is still "
              "open — re-measure 'start' to the reveal, or chat can copy the host.")
    return out or None


# ------------------------------- the scorer -------------------------------

class Scorer:
    def __init__(self, service, liveChatId, config):
        self.service = service
        self.liveChatId = liveChatId
        self.window_seconds = config.get("window_seconds", 25)
        self.points = config.get("points", [10, 8, 7, 6, 5, 4, 3, 2])
        self.min_points = config.get("min_points", 1)

        # Cumulative scores keyed by channelId (stable even if a display name
        # repeats or changes); we keep the latest display name for rendering.
        self.scores = {}            # channelId -> total points
        self.names = {}             # channelId -> display name

        # Per-question state, guarded by the lock.
        self.lock = threading.Lock()
        self.window_open = False
        self.window_open_at = None  # datetime (UTC) the window opened
        self.current_answers = []
        self.correct_count = 0      # how many have scored this question (for decay)
        self.scored_channels = set()  # channelIds that already scored this question

        self.page_token = None
        self.running = True

    # ----- leaderboard rendering -----

    def leaderboard(self, top=5):
        rows = [{"name": self.names.get(cid, "?"), "score": s}
                for cid, s in self.scores.items()]
        rows.sort(key=lambda r: r["score"], reverse=True)
        return rows[:top]

    def publish_state(self, question=None, window_ends_at=None, phase="question"):
        write_state({
            "phase": phase,
            "question": question,
            "window_ends_at": window_ends_at,
            "leaderboard": self.leaderboard(),
        })

    # ----- points for the next correct answer (speed decay) -----

    def next_points(self):
        i = self.correct_count
        return self.points[i] if i < len(self.points) else self.min_points

    # ----- background polling -----

    def poll_loop(self):
        """Continuously pull chat. Score only while a window is open, and only
        messages published after the window opened (so backlog never scores)."""
        # Prime the page token to skip pre-existing history.
        try:
            first = self.service.liveChatMessages().list(
                liveChatId=self.liveChatId, part="id").execute()
            self.page_token = first.get("nextPageToken")
            interval = first.get("pollingIntervalMillis", 2000) / 1000.0
        except Exception as e:
            print(f"\n[poll] could not start: {e}")
            self.running = False
            return

        while self.running:
            try:
                resp = self.service.liveChatMessages().list(
                    liveChatId=self.liveChatId,
                    part="snippet,authorDetails",
                    pageToken=self.page_token,
                ).execute()
            except Exception as e:
                print(f"\n[poll] error (will retry): {e}")
                time.sleep(3)
                continue

            self.page_token = resp.get("nextPageToken")
            interval = resp.get("pollingIntervalMillis", 2000) / 1000.0

            for item in resp.get("items", []):
                self.handle_message(item)

            time.sleep(interval)

    def handle_message(self, item):
        snippet = item.get("snippet", {})
        author = item.get("authorDetails", {})
        text = snippet.get("displayMessage", "")
        cid = author.get("channelId")
        name = author.get("displayName", "?")
        published = snippet.get("publishedAt")  # ISO 8601, UTC

        if not text or not cid:
            return

        with self.lock:
            if not self.window_open:
                return
            # Only count messages sent after the window opened.
            if published:
                try:
                    ts = datetime.fromisoformat(published.replace("Z", "+00:00"))
                    if ts < self.window_open_at:
                        return
                except ValueError:
                    pass
            if cid in self.scored_channels:
                return  # one scoring chance per person per question
            if matches(text, self.current_answers):
                pts = self.next_points()
                self.correct_count += 1
                self.scored_channels.add(cid)
                self.scores[cid] = self.scores.get(cid, 0) + pts
                self.names[cid] = name
                rank = self.correct_count
                print(f"  ✓ {name} +{pts}  (#{rank} correct)  total={self.scores[cid]}")
                # Push the updated leaderboard out immediately.
                self.publish_state(
                    question=self._q, window_ends_at=self._ends, phase="question")

    # ----- opening / closing an answer window -----

    _q = None
    _ends = None

    def _open(self, q, close_epoch, text=None):
        """Open q's window NOW; tell the overlay it closes at close_epoch
        (absolute epoch seconds, so the countdown ring shows the real
        remaining time whatever the window's length).

        `text` is what the lower-third shows right now — by default BOTH spoken
        hints (never the spare, which the host doesn't read), or "" when hint
        marks will reveal them one at a time later in the window (the overlay
        hides the bar on empty text). Scoring is live from this instant either
        way: an empty bar means the host hasn't given the first hint yet, not
        that answers are closed."""
        with self.lock:
            self.window_open = True
            self.window_open_at = datetime.now(timezone.utc)
            self.current_answers = q["answers"]
            self.correct_count = 0
            self.scored_channels = set()
            self._q = {"number": q.get("number"), "total": self._total,
                       "text": "  •  ".join(spoken_hints(q))
                               if text is None else text}
            self._ends = close_epoch
            self.publish_state(question=self._q, window_ends_at=close_epoch,
                               phase="question")
        remaining = max(0.0, close_epoch - time.time())
        print(f"\nQ{q.get('number')}: {q.get('word', q['text'])}")
        print(f"Window open for {remaining:.0f}s. Accepted: {q['answers']}")

    def _show(self, text):
        """Republish the open question with more hint text on screen. Only the
        lower-third changes — the window, the clock and the scores don't."""
        with self.lock:
            self._q = dict(self._q, text=text)
            self.publish_state(question=self._q, window_ends_at=self._ends,
                               phase="question")

    def _close(self):
        with self.lock:
            self.window_open = False
            self._ends = None
            self.publish_state(question=self._q, window_ends_at=None,
                               phase="reveal")
        print(f"Time! {self.correct_count} correct answer(s) this question.")

    def open_question(self, q):
        """FIXED mode: open now, run window_seconds, close. Windows are
        back-to-back, so the reveal of q lands as the next window opens."""
        close_epoch = time.time() + self.window_seconds
        self._open(q, close_epoch)
        time.sleep(max(0.0, close_epoch - time.time()))
        self._close()

    def run_timeline(self, questions, t0, starts, outro_start, hint_marks=None):
        """TIMELINE mode: open each window at its measured second-mark
        (t0 + starts[i]) and close it when the next window opens — for the
        last, at outro_start. t0 is the wall-clock instant of the video-start
        sync press, so all marks are anchored to the same clock the chat
        timestamps use. The gaps before Q1 (intro) and between windows are
        just waited out; the board stays on its last state meanwhile.

        A window opens on the host REVEALING THE PREVIOUS WORD, several seconds
        before he gives this word's first hint — so with hint_marks the bar
        starts empty, the teaser appears when spoken, and the long hint then
        replaces it. Without marks both hints sit on screen from the open,
        which hands chat the closer early: fill the marks before a real show."""
        bounds = list(starts) + [outro_start]
        for i, q in enumerate(questions):
            open_epoch = t0 + bounds[i]
            close_epoch = t0 + bounds[i + 1]
            wait = open_epoch - time.time()
            if wait > 0:
                time.sleep(wait)
            elif wait < -1:
                print(f"  (running {-wait:.0f}s behind the mark for "
                      f"Q{q.get('number')} — check the sync press)")

            marks = hint_marks[i] if hint_marks else None
            self._open(q, close_epoch, text="" if marks else None)

            if marks:
                # One hint on screen at a time, each REPLACING the last at the
                # second the host says it. Not cumulative: the two stacked make
                # the lower-third tall enough to collide with the leaderboard,
                # and the screen should follow the voice — when he moves to the
                # long hint, so does the bar.
                hints = spoken_hints(q)
                for n, mark in enumerate(marks[:len(hints)], 1):
                    gap = (t0 + mark) - time.time()
                    if gap > 0:
                        time.sleep(gap)
                    self._show(hints[n - 1])
                    print(f"  hint {n}/{len(hints)}: {hints[n - 1]}")

            rest = close_epoch - time.time()
            if rest > 0:
                time.sleep(rest)
            self._close()


# --------------------------------- main ---------------------------------

def find_live_chat_id(service):
    """Return the liveChatId of the user's active broadcast, or None."""
    resp = service.liveBroadcasts().list(
        part="snippet,status", broadcastStatus="active",
        broadcastType="all").execute()
    items = resp.get("items", [])
    if not items:
        return None
    return items[0]["snippet"].get("liveChatId")


def main():
    config = load_questions()
    questions = config["questions"]

    print("Authenticating with YouTube ...")
    service = get_service()

    print("Finding your active broadcast ...")
    chat_id = find_live_chat_id(service)
    if not chat_id:
        print("No active broadcast found. Start your live stream first "
              "(it must be live, not just scheduled), then run this again.")
        sys.exit(1)
    print(f"Connected to live chat: {chat_id}")

    scorer = Scorer(service, chat_id, config)
    scorer._total = len(questions)

    # Show an empty board right away.
    scorer.publish_state(question=None, window_ends_at=None, phase="idle")

    poller = threading.Thread(target=scorer.poll_loop, daemon=True)
    poller.start()

    timeline = load_timeline(len(questions))
    if timeline:
        print(f"\nTIMELINE mode: {len(questions)} windows measured from the video "
              f"(Q1 opens at {timeline['starts'][0]:.0f}s, last closes at "
              f"{timeline['outro_start']:.0f}s).")
        print("Hints: revealed on screen as the host speaks them."
              if timeline["hints"] else
              "Hints: ALL shown at window open (no 'hints' marks in timeline.json).")
    else:
        print(f"\nFIXED mode: {scorer.window_seconds}s per window, back-to-back "
              "(no valid timeline.json).")

    input("\nPress Enter the moment the video starts to sync the show  >  ")
    t0 = time.time()
    if timeline:
        scorer.run_timeline(questions, t0, timeline["starts"],
                            timeline["outro_start"], timeline["hints"])
    else:
        for q in questions:
            scorer.open_question(q)  # blocks for window_seconds, then auto-advances

    scorer.running = False
    print("\nFinal leaderboard:")
    for i, row in enumerate(scorer.leaderboard(), 1):
        print(f"  {i}. {row['name']} — {row['score']}")
    print("Done.")


if __name__ == "__main__":
    main()
