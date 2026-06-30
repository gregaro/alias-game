"""
chat_scorer.py — read YouTube live chat, score answers, drive the overlay.

Closes the loop: viewers' chat answers move the leaderboard the overlay shows.

How it works
------------
- Authenticates with your existing youtube_auth.get_service().
- Finds your active broadcast's liveChatId.
- Runs a background thread that polls liveChatMessages.list at the interval the
  API tells it to (pollingIntervalMillis).
- You drive the show from the terminal: press Enter to open the next question's
  answer window. While a window is open, correct answers score with speed decay
  (1st correct = most points, then less). Scores are cumulative across questions.
- After every scoring change it writes state.json — the SAME file your overlay
  server reads — so names pop onto the leaderboard live.

This is the Phase 4 / Phase 5 orchestrator in its simplest honest form: you are
the one advancing questions. Automating that sequence is a backlog item.

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

    def leaderboard(self, top=10):
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

    def open_question(self, q):
        ends = time.time() + self.window_seconds
        with self.lock:
            self.window_open = True
            self.window_open_at = datetime.now(timezone.utc)
            self.current_answers = q["answers"]
            self.correct_count = 0
            self.scored_channels = set()
            self._q = {"number": q.get("number"), "total": self._total,
                       "text": q["text"]}
            self._ends = ends
            self.publish_state(question=self._q, window_ends_at=ends,
                               phase="question")
        print(f"\nQ{q.get('number')}: {q['text']}")
        print(f"Window open for {self.window_seconds}s. Accepted: {q['answers']}")

        # Let it run for the window, then close.
        time.sleep(self.window_seconds)
        with self.lock:
            self.window_open = False
            self._ends = None
            self.publish_state(question=self._q, window_ends_at=None,
                               phase="reveal")
        print(f"Time! {self.correct_count} correct answer(s) this question.")


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

    print("\nReady. Press Enter to open each question, or 'q' then Enter to quit.")
    idx = 0
    while idx < len(questions):
        cmd = input(f"\n[Enter] open Q{questions[idx].get('number')}  >  ")
        if cmd.strip().lower() == "q":
            break
        scorer.open_question(questions[idx])
        idx += 1

    scorer.running = False
    print("\nFinal leaderboard:")
    for i, row in enumerate(scorer.leaderboard(), 1):
        print(f"  {i}. {row['name']} — {row['score']}")
    print("Done.")


if __name__ == "__main__":
    main()
