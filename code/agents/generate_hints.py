"""Stage two of the show pipeline: latest word set -> hints for every word.

research_words.py saves the word set to the DB; this reads it back and
runs hint_generator over each word. The combined show-ready material
([{word, source_topic, why_fun, hints}]) is saved to the DB under
hint_generator/last_hints — the words -> questions.json step will read it
from there. One failed word is a warning, not an error: 9 playable words
still make a show.

Hints go into avatar TTS, so read the printed output before recording.
A hint that contains its own target word is flagged (not fatal — the
model's hard rule, but reasoning models occasionally slip): re-run or
edit by hand.
"""
import json
import sys
import unicodedata

from dotenv import load_dotenv

load_dotenv()  # load API keys before building any model

import db
from orchestrator import Orchestrator


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold()


def leak_check(word: str, hints: list[str]) -> list[str]:
    """A hint must never contain the target word (or any word of a phrase)."""
    return [
        f"hint {i} contains '{part}'"
        for i, hint in enumerate(hints, 1)
        for part in _norm(word).split()
        if part in _norm(hint)
    ]


def main():
    word_set = db.latest_state("trend_researcher", "last_output")
    if not word_set or not word_set.get("words"):
        raise SystemExit("No word set in the DB — run research_words.py first.")

    orch = Orchestrator()
    show, flags = [], []
    for entry in word_set["words"]:
        word = entry["word"]
        try:
            # persist=False: one combined save at the end beats 10 rows of
            # per-word last_output that would shadow each other.
            result = orch.run("hint_generator", f"WORD: {word}", persist=False)
            show.append({**entry, "hints": result["hints"]})
            flags += [f"{word}: {leak}" for leak in leak_check(word, result["hints"])]
        except Exception as exc:
            print(f"warning: hints failed for {word!r}: {exc}", file=sys.stderr)

    if not show:
        raise SystemExit("Hint generation failed for every word — check the API.")

    db.save_state("hint_generator", "last_hints", show)
    print(f"Hints for {len(show)}/{len(word_set['words'])} words "
          f"(saved to DB as hint_generator/last_hints):\n")
    for entry in show:
        print(f"  {entry['word']}")
        for i, hint in enumerate(entry["hints"], 1):
            print(f"    {i}. {hint}")
        print()

    if flags:
        print("REVIEW NEEDED — these hints leak their target word:")
        for flag in flags:
            print(f"  - {flag}")


if __name__ == "__main__":
    main()
