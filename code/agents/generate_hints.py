"""Stage two of the show pipeline: latest word set -> hints for every word.

research_words.py saves the word set to the DB; this reads it back and
runs hint_generator over each word. The combined show-ready material
([{word, source_topic, why_fun, hints}]) is saved to the DB under
hint_generator/last_hints — the words -> questions.json step will read it
from there. One failed word is a warning, not an error: 9 playable words
still make a show.
"""
import json
import sys

from dotenv import load_dotenv

load_dotenv()  # load API keys before building any model

import db
from orchestrator import Orchestrator


def main():
    word_set = db.latest_state("trend_researcher", "last_output")
    if not word_set or not word_set.get("words"):
        raise SystemExit("No word set in the DB — run research_words.py first.")

    orch = Orchestrator()
    show = []
    for entry in word_set["words"]:
        word = entry["word"]
        try:
            # persist=False: one combined save at the end beats 10 rows of
            # per-word last_output that would shadow each other.
            result = orch.run("hint_generator", f"WORD: {word}", persist=False)
            show.append({**entry, "hints": result["hints"]})
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


if __name__ == "__main__":
    main()
