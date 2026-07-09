"""Stage four of the show pipeline: hints -> the clip script the host reads.

The show_scripter agent writes the frame (intro, per-word lead-ins,
reveals, outro) in the same party-friend voice as the hints; this script
weaves the existing hints into it and writes two artifacts next to
questions.json:

  show_script.json  structured segments, for future TTS/clip automation
  show_script.txt   human-readable, with [PAUSE]/[WINDOW] markers — what
                    you actually feed to ElevenLabs clip by clip

Each word is a 2-hint round inside ONE window_seconds window: the teaser
opens it, a 3-4s beat later the confident closer lands, then guessing
time until the window closes. The concrete middle hint is kept as
spare_hint (never read on air).

Reveals never interrupt a scored window: a word's reveal is spoken at the
START of the NEXT word's turn (host says the answer, then launches the
next word), and the last word's reveal is folded into the outro. So the
on-air flow is intro -> (10 x 20s windows) -> outro, with each answer
announced as the following word begins. Timing is symbolic: real offsets
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


def episode_words(frame: dict, hints_by_word: dict) -> list[dict]:
    """Two hints per round: the teaser opens the window, a 3-4s beat later
    the confident closer lands, and the whole word runs inside one window.
    The concrete middle hint stays as spare material (never read on air)."""
    words = []
    for w in frame["words"]:
        hints = hints_by_word[w["word"]]
        words.append({
            "word": w["word"],
            "lead_in": w["lead_in"],
            "teaser": hints[0],
            "closer": hints[-1],
            "spare_hint": hints[1] if len(hints) > 2 else None,
            "reveal": w["reveal"],
        })
    return words


def assemble_txt(intro: str, words: list[dict], outro: str,
                 window_seconds: int) -> str:
    lines = ["=== INTRO ===", intro, ""]
    for i, w in enumerate(words, 1):
        lines.append(f"=== WORD {i}: {w['word']} ===")
        if i > 1:
            # Option C: announce the previous word's answer, THEN launch this
            # one — all before the window opens, so no reveal lands mid-window.
            lines.append(f"(reveal prev)  {words[i - 2]['reveal']}")
        lines += [
            w["lead_in"],
            f"[WINDOW OPENS — {window_seconds}s total]",
            w["teaser"],
            "[PAUSE 3-4s]",
            w["closer"],
            "[guessing until the window closes — no reveal yet]",
            "",
        ]
    # The last word has no next turn to carry its reveal, so it opens the outro.
    lines += ["=== OUTRO ===",
              f"(reveal prev)  {words[-1]['reveal']}",
              outro, ""]
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
    words = episode_words(frame, hints_by_word)
    script = {"window_seconds": window_seconds, "intro": frame["intro"],
              "words": words, "outro": frame["outro"]}
    _write_atomic(OUT_JSON, json.dumps(script, ensure_ascii=False, indent=2) + "\n")
    txt = assemble_txt(frame["intro"], words, frame["outro"], window_seconds)
    _write_atomic(OUT_TXT, txt)

    print(f"Wrote {OUT_TXT} and {OUT_JSON}\n")
    print(txt)


if __name__ == "__main__":
    main()
