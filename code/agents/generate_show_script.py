"""Stage four of the show pipeline: hints -> the clip script the host reads.

The show_scripter agent writes the frame (intro, per-word lead-ins,
reveals, outro) in the same party-friend voice as the hints; this script
weaves the existing hints into it and writes two artifacts next to
questions.json:

  show_script.json  structured segments, for future TTS/clip automation
  show_script.txt   human-readable, with [PAUSE]/[WINDOW] markers — what
                    you actually feed to ElevenLabs clip by clip

Timing is symbolic ([PAUSE 3s], window open/close markers): real offsets
only exist once TTS audio exists, so the scorer sync stays manual-Enter
until then.
"""
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()  # load API keys before building any model

import db
from orchestrator import Orchestrator

HERE = os.path.dirname(os.path.abspath(__file__))
QUESTIONS_FILE = os.path.join(HERE, "../questions/questions.json")
OUT_JSON = os.path.join(HERE, "../questions/show_script.json")
OUT_TXT = os.path.join(HERE, "../questions/show_script.txt")


def _write_atomic(path: str, text: str):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def assemble_txt(frame: dict, hints_by_word: dict, window_seconds: int) -> str:
    lines = ["=== INTRO ===", frame["intro"], ""]
    for i, w in enumerate(frame["words"], 1):
        lines += [
            f"=== WORD {i}: {w['word']} ===",
            w["lead_in"],
            f"[WINDOW OPENS — {window_seconds}s]",
        ]
        for hint in hints_by_word[w["word"]]:
            lines += [hint, "[PAUSE 3s]"]
        lines[-1] = "[WAIT until window closes]"  # after the last hint
        lines += [w["reveal"], ""]
    lines += ["=== OUTRO ===", frame["outro"], ""]
    return "\n".join(lines)


def main():
    show = db.latest_state("hint_generator", "last_hints")
    if not show:
        raise SystemExit("No hints in the DB — run generate_hints.py first.")
    hints_by_word = {e["word"]: e["hints"] for e in show}

    window_seconds = 25
    if os.path.exists(QUESTIONS_FILE):
        with open(QUESTIONS_FILE, encoding="utf-8") as f:
            window_seconds = json.load(f).get("window_seconds", 25)

    orch = Orchestrator()
    frame = orch.run(
        "show_scripter",
        "Write the host frame for this episode.",
        context={"words": show, "window_seconds": window_seconds},
        persist=False,
    )

    # The frame must cover exactly our words, in order — a mismatch means
    # hallucinated or dropped words, and the clip script would desync.
    got = [w["word"] for w in frame.get("words", [])]
    want = [e["word"] for e in show]
    if got != want:
        raise SystemExit(f"Frame word list mismatch:\n  want {want}\n  got  {got}")

    # lead_ins must not leak any target word of the episode.
    for w in frame["words"]:
        for target in want:
            if target.casefold() in w["lead_in"].casefold():
                print(f"warning: lead_in for {w['word']!r} mentions {target!r} "
                      "— review before recording", file=sys.stderr)

    db.save_state("show_scripter", "last_script", frame)
    _write_atomic(OUT_JSON, json.dumps(
        {"window_seconds": window_seconds, **frame},
        ensure_ascii=False, indent=2) + "\n")
    txt = assemble_txt(frame, hints_by_word, window_seconds)
    _write_atomic(OUT_TXT, txt)

    print(f"Wrote {OUT_TXT} and {OUT_JSON}\n")
    print(txt)


if __name__ == "__main__":
    main()
