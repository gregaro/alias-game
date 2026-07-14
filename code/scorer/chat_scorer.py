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

import argparse
import json
import os
import sys
import time
import threading
from datetime import datetime, timedelta, timezone

from youtube_auth import get_service
from normalize import matches

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))   # code/, for episode.py
import episode

STATE_FILE = os.path.join(HERE, "../overlay/state.json")
LOG_DIR = os.path.join(HERE, "../logs")   # gitignored: run artifacts, per episode

# Set from the resolved episode in main(); the loaders read them. Every show's
# files live in their own folder, so an old episode stays runnable.
QUESTIONS_FILE = None
TIMELINE_FILE = None

# How long an announced answer stays on the lower-third before the bar clears.
# Long enough to read, short enough that it isn't still sitting there under the
# host's lead-in to the next word — or under the whole outro.
ANSWER_HOLD_SECONDS = 5.0

# How long after a window shuts we keep WATCHING for its answer. Nothing scores
# here — it is a measurement. Viewers run 3-10s behind the stream, so someone who
# only gets the word from the long hint can type it a beat too late; they are
# invisible otherwise. If a rehearsal shows a cluster of these, the fix is more
# silence after the hint in the render, NOT a longer window here (the window ends
# where the host says the answer, and moving it lets chat copy him).
LATE_GRACE_SECONDS = 20.0


# ------------------------------ the event log ------------------------------

class EventLog:
    """Append-only JSONL record of a run: one object per line, flushed as it
    happens so a crash mid-show still leaves everything up to that point.

    This is the audit trail AND the tuning instrument. The most valuable rows
    are the ones nothing else keeps: every chat message that did NOT match, with
    the word that was open at the time. Those are the missing spellings in
    answers[] — a viewer who knew the word and scored zero — and they are
    invisible in the console. After a show, run review_log.py to pull them out.
    """

    def __init__(self, path):
        self.path = path
        self.t0 = None                   # video-start sync, set at the Enter press
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.f = open(path, "a", encoding="utf-8", buffering=1)

    def write(self, event, **fields):
        row = {"at": datetime.now(timezone.utc).isoformat(), "event": event}
        # Seconds into the video, so a row can be found in the rendered clip.
        if self.t0 is not None:
            row["video_t"] = round(time.time() - self.t0, 1)
        row.update(fields)
        with self.lock:
            self.f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def close(self):
        with self.lock:
            self.f.close()


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


def answer_text(q) -> str:
    """What the lower-third says while the host announces this word's answer.
    Armenian, because the host is: "The correct answer: <word>"."""
    return "Ճիշտ պատասխանը՝ " + q.get("word", q["answers"][0])


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
    def __init__(self, service, liveChatId, config, log=None):
        self.service = service
        self.liveChatId = liveChatId
        self.log = log
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
        self.hint_at = None         # datetime the host speaks the long hint
        self.word = None            # current word, for the timing report

        # Rehearsal instrumentation. Pure observers: they change nothing about
        # what scores or what the overlay shows.
        #
        # `late` is the important one. A correct answer arriving after the window
        # shuts is dropped on the floor — so the viewers our pacing FAILED are
        # exactly the ones who leave no trace, and a rehearsal would look clean
        # while quietly losing people. Recording them is the only way to know
        # whether the silence after the long hint is long enough.
        self.solves = []            # {word, after_open, after_hint}
        self.late = []              # {word, name, seconds_late}
        self._closed = None         # just-closed window, kept for the grace check

        self.page_token = None
        self.running = True

    # ----- rehearsal timing report -----

    def timing_report(self):
        """What the rehearsal is FOR: did people have enough time to type?

        Solves are split either side of the long hint, because that is the
        pacing question — if almost everyone only lands after it, the teaser is
        too hard; if people are still arriving as the window shuts, the silence
        after the hint is too short. The late list is the smoking gun: those
        viewers knew the word and got nothing."""
        print("\n--- timing ---")
        if not self.solves and not self.late:
            print("No correct answers to time.")
            return

        on_teaser = [s for s in self.solves if s["after_hint"] is not None
                     and s["after_hint"] < 0]
        on_hint = [s for s in self.solves if s["after_hint"] is not None
                   and s["after_hint"] >= 0]
        print(f"Solved on the teaser: {len(on_teaser)}   "
              f"after the long hint: {len(on_hint)}")
        if on_hint:
            secs = sorted(s["after_hint"] for s in on_hint)
            mid = secs[len(secs) // 2]
            print(f"  of those, they took {min(secs):.0f}-{max(secs):.0f}s after "
                  f"the hint (median {mid:.0f}s)")

        if not self.late:
            print("\nNobody answered correctly after a window closed — the "
                  "silence after the long hint is long enough.")
            return
        print(f"\n{len(self.late)} correct answer(s) arrived TOO LATE and scored "
              "nothing:")
        by_word = {}
        for l in self.late:
            by_word.setdefault(l["word"], []).append(l["seconds_late"])
        for word, lates in by_word.items():
            print(f"  {word}: {len(lates)} missed by "
                  f"{min(lates):.1f}-{max(lates):.1f}s")
        worst = max(l["seconds_late"] for l in self.late)
        print(f"\nAdd ~{worst + 2:.0f}s more silence after the long hint in the "
              "render to catch them. Do NOT lengthen the window instead — it "
              "ends where the host says the answer, and moving it lets chat "
              "copy him.")

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

        # The send time, not the poll time: polling lags by seconds and would
        # smear every measurement below.
        ts = None
        if published:
            try:
                ts = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                pass

        # Every message ends up with exactly one verdict, and every verdict is
        # logged — including the boring ones. `no_match` is the whole point: a
        # viewer typing a spelling we forgot looks identical to idle chatter
        # here, and only shows up when you read the log afterwards.
        with self.lock:
            extra = {}

            # Windows are CONTIGUOUS — one closes exactly as the next opens — so
            # a straggler still typing the previous word arrives while the new
            # window is already open. The late check therefore cannot hang off
            # `window_open`; it has to run on every message, against the word
            # that just closed. (The two answer sets never overlap: stage 3
            # rejects a variant that would match two words.)
            late = self._note_late(cid, name, text, ts)
            if late:
                verdict, word, extra = "late", late["word"], {
                    "seconds_late": round(late["seconds_late"], 1)}
            elif not self.window_open:
                verdict, word = "no_window", None
            elif ts and ts < self.window_open_at:
                verdict, word = "pre_window", self.word   # backlog, never scores
            elif matches(text, self.current_answers):
                if cid in self.scored_channels:
                    verdict, word = "repeat", self.word   # already scored it
                else:
                    pts = self.next_points()
                    self.correct_count += 1
                    self.scored_channels.add(cid)
                    self.scores[cid] = self.scores.get(cid, 0) + pts
                    self.names[cid] = name
                    rank = self.correct_count
                    print(f"  ✓ {name} +{pts}  (#{rank} correct)  "
                          f"total={self.scores[cid]}")
                    solve = self._note_solve(ts)
                    verdict, word = "scored", self.word
                    extra = {"points": pts, "rank": rank,
                             "total": self.scores[cid]}
                    if solve:
                        extra["after_open"] = round(solve["after_open"], 1)
                        if solve["after_hint"] is not None:
                            extra["after_hint"] = round(solve["after_hint"], 1)
                    # Push the updated leaderboard out immediately.
                    self.publish_state(question=self._q, window_ends_at=self._ends,
                                       phase="question")
            else:
                verdict, word = "no_match", self.word

        if self.log:
            self.log.write("chat", verdict=verdict, word=word, name=name,
                           channel=cid, text=text, sent=published, **extra)

    # ----- rehearsal instrumentation (observers: they never score) -----

    def _note_solve(self, ts):
        """Record WHEN this solve landed — before the long hint (the teaser was
        enough) or after it. Caller holds the lock. Returns the record, or None
        if the message carried no usable timestamp."""
        if not ts or not self.window_open_at:
            return None
        after_hint = (ts - self.hint_at).total_seconds() if self.hint_at else None
        rec = {
            "word": self.word,
            "after_open": (ts - self.window_open_at).total_seconds(),
            "after_hint": after_hint,
        }
        self.solves.append(rec)
        return rec

    def _note_late(self, cid, name, text, ts):
        """A correct answer that arrived after its window shut. Recorded ONLY —
        never scored, or chat could answer while the host is saying the word.
        Caller holds the lock. Returns the record, or None if this isn't one."""
        c = self._closed
        if not c or not ts:
            return None
        seconds_late = (ts - c["closed_at"]).total_seconds()
        if not 0 < seconds_late <= LATE_GRACE_SECONDS:
            return None
        if cid in c["scored"] or cid in c["noted"]:
            return None  # already scored in time, or already counted once as late
        if not matches(text, c["answers"]):
            return None
        c["noted"].add(cid)     # count a person once, not once per repeat message
        rec = {"word": c["word"], "name": name, "seconds_late": seconds_late}
        self.late.append(rec)
        print(f"  ⏱ LATE  {name} had «{c['word']}» {seconds_late:.1f}s after the "
              "window closed — no points")
        return rec

    # ----- opening / closing an answer window -----
    #
    # SCORING and DISPLAY run on different clocks, and keeping them apart is the
    # whole trick here.
    #
    # Scoring covers the FULL window (start[i] -> start[i+1]), because that is
    # exactly the span in which the word's answer is still secret — it stops the
    # instant the host says it out loud.
    #
    # The overlay follows the host's voice instead. A window's first seconds are
    # the host announcing the PREVIOUS word's answer, so that is what the bar
    # shows, with no ring: there is nothing to guess yet. The countdown only
    # starts at the teaser and runs to the end of the window.

    _q = None
    _ends = None

    def _open(self, q, close_epoch, hint_epoch=None):
        """Start scoring q. Deliberately publishes NOTHING — the caller decides
        what is on screen, because at this instant the host is still announcing
        the previous word's answer, not asking this one.

        `hint_epoch` is when the host speaks the long hint; it only tags solves
        as before/after it in the timing report."""
        now = datetime.now(timezone.utc)
        with self.lock:
            self.window_open = True
            self.window_open_at = now
            self.current_answers = q["answers"]
            self.correct_count = 0
            self.scored_channels = set()
            self.word = q.get("word", q["text"])
            self.hint_at = (now + timedelta(seconds=hint_epoch - time.time())
                            if hint_epoch else None)
        remaining = max(0.0, close_epoch - time.time())
        print(f"\nQ{q.get('number')}: {q.get('word', q['text'])}")
        print(f"Scoring open for {remaining:.0f}s. Accepted: {q['answers']}")
        if self.log:
            self.log.write("window_open", n=q.get("number"), word=self.word,
                           seconds=round(remaining, 1), answers=q["answers"])

    def _display(self, number, text, ends=None, phase="question"):
        """Set the lower-third. `ends` is the countdown ring's target (epoch
        seconds) or None to hide the ring entirely. Passing the SAME `ends`
        across calls lets the text change without disturbing the countdown —
        that is how the long hint replaces the teaser mid-window."""
        with self.lock:
            self._q = {"number": number, "total": self._total, "text": text}
            self._ends = ends
            self.publish_state(question=self._q, window_ends_at=ends,
                               phase=phase)

    def _close(self):
        """Stop scoring. Leaves the bar alone: whoever opens the next window
        puts this word's answer up, which is the same instant the host says it."""
        with self.lock:
            self.window_open = False
            # Keep the word watchable for a few seconds AFTER it stops scoring,
            # so a correct answer that missed the cut is counted (never scored).
            self._closed = {
                "word": self.word,
                "answers": self.current_answers,
                "scored": set(self.scored_channels),
                "noted": set(),
                "closed_at": datetime.now(timezone.utc),
            }
        print(f"Time! {self.correct_count} correct answer(s) this question.")
        if self.log:
            self.log.write("window_close", word=self._closed["word"],
                           correct=self.correct_count)

    def open_question(self, q):
        """FIXED mode: open now, run window_seconds, close, show the answer."""
        close_epoch = time.time() + self.window_seconds
        self._open(q, close_epoch)
        self._display(q.get("number"), "  •  ".join(spoken_hints(q)), close_epoch)
        time.sleep(max(0.0, close_epoch - time.time()))
        self._close()
        self._display(q.get("number"), answer_text(q), None, phase="reveal")

    def run_timeline(self, questions, t0, starts, outro_start, hint_marks=None):
        """TIMELINE mode: open each window at its measured second-mark
        (t0 + starts[i]) and close it when the next window opens — for the
        last, at outro_start. t0 is the wall-clock instant of the video-start
        sync press, so all marks are anchored to the same clock the chat
        timestamps use. The gaps before Q1 (intro) and between windows are
        just waited out; the board stays on its last state meanwhile.

        With hint_marks the bar tracks the host's VOICE through each window: the
        previous word's answer while he announces it, then the teaser, then the
        long hint replacing it. The ring runs teaser -> end of window. Without
        marks both hints sit on screen from the open, which hands chat the
        closer early: fill the marks before a real show."""
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
            # marks is (teaser, hint); the second is the long hint most people
            # solve on, so the report can split solves either side of it.
            self._open(q, close_epoch,
                       t0 + marks[1] if marks and len(marks) > 1 else None)

            if not marks:
                self._display(q.get("number"),
                              "  •  ".join(spoken_hints(q)), close_epoch)
            else:
                # The window opens ON the host announcing the previous word's
                # answer, so put that answer up — and keep the ring OFF: this
                # word's clock hasn't started, there is nothing to guess yet.
                if i:
                    prev = questions[i - 1]
                    self._display(prev.get("number"), answer_text(prev),
                                  None, phase="reveal")
                    print(f"  answer to Q{prev.get('number')}: {prev['word']}")
                    # Clear it after a beat: the host has moved on to the lead-in
                    # by then, and a stale answer under a new word reads as a
                    # hint. If the teaser lands first it just replaces it.
                    hide_at = min(open_epoch + ANSWER_HOLD_SECONDS,
                                  t0 + marks[0])
                    time.sleep(max(0.0, hide_at - time.time()))
                    self._display(None, "", None)
                else:
                    self._display(None, "", None)   # Q1: nothing precedes it

                # Then one hint at a time, each REPLACING the last as the host
                # says it. Passing the same close_epoch each time starts the ring
                # at the teaser and lets it run on undisturbed through the swap
                # to the long hint.
                hints = spoken_hints(q)
                for n, mark in enumerate(marks[:len(hints)], 1):
                    gap = (t0 + mark) - time.time()
                    if gap > 0:
                        time.sleep(gap)
                    self._display(q.get("number"), hints[n - 1], close_epoch)
                    print(f"  hint {n}/{len(hints)}: {hints[n - 1]}")
                    if self.log:
                        # "teaser" then "hint", matching timeline.json's names.
                        self.log.write(
                            "hint_shown", word=self.word,
                            which="teaser" if n == 1 else "hint",
                            text=hints[n - 1])

            rest = close_epoch - time.time()
            if rest > 0:
                time.sleep(rest)
            self._close()

        # No window follows the last one to announce its answer, so do it here —
        # outro_start is exactly where the host's final reveal lands. Then clear
        # the bar: the outro is about the winners, so leave the board alone on
        # screen rather than a dead answer pinned under it.
        last = questions[-1]
        self._display(last.get("number"), answer_text(last), None, phase="reveal")
        print(f"  answer to Q{last.get('number')}: {last['word']}")
        time.sleep(ANSWER_HOLD_SECONDS)
        self._display(None, "", None, phase="reveal")
        print("\nOutro: board only, question bar cleared.")


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
    global QUESTIONS_FILE, TIMELINE_FILE
    parser = argparse.ArgumentParser(description="Score a live episode from chat.")
    episode.add_argument(parser)
    args = parser.parse_args()

    # Resolve the episode BEFORE anything else: running the wrong show is worse
    # than not running at all, so print which one loudly and fail hard if the
    # name is unknown.
    name = args.episode or episode.current()
    ep = episode.resolve(name)
    QUESTIONS_FILE = str(ep / episode.QUESTIONS)
    TIMELINE_FILE = str(ep / episode.TIMELINE)
    print(f"Episode: {name}"
          f"{'  (from current_episode)' if not args.episode else '  (--episode)'}")

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

    # One log per RUN, not per episode: a rehearsal and the real show are two
    # different sets of facts and neither should overwrite the other.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log = EventLog(os.path.join(LOG_DIR, name, f"{stamp}.jsonl"))
    print(f"Logging to: {log.path}")

    scorer = Scorer(service, chat_id, config, log=log)
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
    # Anchor every later row to the video, so a row can be replayed against the
    # rendered clip: video_t is seconds from this press.
    log.t0 = t0
    log.write("show_start", episode=name, questions=len(questions),
              mode="TIMELINE" if timeline else "FIXED",
              words=[q.get("word") for q in questions])

    try:
        if timeline:
            scorer.run_timeline(questions, t0, timeline["starts"],
                                timeline["outro_start"], timeline["hints"])
        else:
            for q in questions:
                scorer.open_question(q)  # blocks window_seconds, then advances

        # The last word closes seconds before we get here, so keep polling through
        # the outro: otherwise its late answers — the ones the report exists to
        # catch — would be cut off with the poller. The outro is playing anyway.
        time.sleep(max(0.0, LATE_GRACE_SECONDS - ANSWER_HOLD_SECONDS))
    except KeyboardInterrupt:
        # A show cut short still has to leave a usable log behind.
        print("\nInterrupted — writing what we have.")
        log.write("interrupted")
    finally:
        scorer.running = False
        board = scorer.leaderboard(top=100)
        log.write("show_end", leaderboard=board,
                  solves=len(scorer.solves), late=len(scorer.late))
        log.close()

    print("\nFinal leaderboard:")
    for i, row in enumerate(scorer.leaderboard(), 1):
        print(f"  {i}. {row['name']} — {row['score']}")
    scorer.timing_report()
    print(f"\nLog: {log.path}")
    print(f"Review it:  python code/scorer/review_log.py {log.path}")
    print("\nDone.")


if __name__ == "__main__":
    main()
