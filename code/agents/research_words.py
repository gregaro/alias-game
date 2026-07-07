"""One command per show: fetch trends -> LLM editor -> playable words.

This is a pipeline, not tool-calling: plain code fetches the topics, the
trend_researcher agent judges them. Each half can be run and inspected on
its own (python fetch_topics.py to eyeball the raw list; re-run this to
re-judge the same day's trends).
"""
import json

from dotenv import load_dotenv

load_dotenv()  # load API keys before building any model

import db
from fetch_topics import fetch_all
from orchestrator import Orchestrator

WORD_COUNT = 10
# Trends season the set; they don't drive it. At most this many words may
# come from the trends feed (0 is fine when the feed is weak) — the rest
# are "wildcards" the editor invents freely from ANY domain.
MAX_TREND_WORDS = 3


def recent_words(max_runs: int = 3) -> list[str]:
    """Words from the last few research runs (one run per show, so this is
    a ~3-show window). Older words are fair game again — with fresh hints
    a reused word is a different puzzle, and an all-time ban starves the
    pool. The skill additionally allows ~1 repeat even inside the window."""
    words: list[str] = []
    for run in db.all_states("trend_researcher", "last_output", limit=max_runs):
        for entry in run.get("words", []):
            if entry["word"] not in words:
                words.append(entry["word"])
    return words


def main():
    topics = fetch_all()
    if not topics:
        raise SystemExit("No topics fetched from any geo — check the network.")

    print(f"Fetched {len(topics)} trending topics:")
    for topic in topics:
        print(f"  - {topic}")

    avoid = recent_words()
    if avoid:
        print(f"\nAvoiding {len(avoid)} recently used words: {', '.join(avoid)}")

    orch = Orchestrator()
    result = orch.run(
        "trend_researcher",
        "Build the word set: mostly wildcard words of your own invention, "
        "seasoned with at most a few strong picks from these trending topics.",
        context={
            "topics": topics,
            "recent_words": avoid,
            "count": WORD_COUNT,
            "max_trend_words": MAX_TREND_WORDS,
        },
    )

    print("\nSelected words:")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
