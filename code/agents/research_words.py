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

WORD_COUNT = 5


def recent_words(max_runs: int = 10) -> list[str]:
    """Words from past research runs, so shows don't repeat themselves."""
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
        "Select the most playable Alias words from these trending topics.",
        context={"topics": topics, "recent_words": avoid, "count": WORD_COUNT},
    )

    print("\nSelected words:")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
