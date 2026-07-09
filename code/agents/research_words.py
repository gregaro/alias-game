"""One command per show: fetch trends -> LLM editor -> playable words.

This is a pipeline, not tool-calling: plain code fetches the topics, the
trend_researcher agent judges them. Each half can be run and inspected on
its own (python fetch_topics.py to eyeball the raw list; re-run this to
re-judge the same day's trends).

Randomness lives in CODE, not in the model. A reasoning model asked the
same question re-derives the same "best" answers every run (temperature
barely moves it), so consecutive runs used to repeat words heavily. Two
counters here: (1) each run samples a few focus_domains that steer the
model toward a different corner of word-space, and (2) the model returns
a large candidate pool from which random.sample() picks the final set.
"""
import json
import random

from dotenv import load_dotenv

load_dotenv()  # load API keys before building any model

import db
from fetch_topics import fetch_all
from orchestrator import Orchestrator

WORD_COUNT = 10        # words per show
CANDIDATE_COUNT = 25   # pool the model returns; code samples the show set
FOCUS_DOMAINS_PER_RUN = 6   # thin spread: a couple of candidates from each,
                            # not a pool themed around 3 big assignments
# Trends season the set; they don't drive it. At most this many words may
# come from the trends feed (0 is fine when the feed is weak) — the rest
# are "wildcards" the editor invents freely from ANY domain.
MAX_TREND_WORDS = 3

# The wildcard universe. Each run samples a few of these as focus_domains —
# a viewer-nameable theme, weighted toward everyday Armenian life. Grown
# from a real Alias deck; add freely, the sampler does the rest.
DOMAINS = [
    "kitchen & cooking",
    "household objects & chores",
    "clothing & accessories",
    "body & health",
    "food & drink (dishes, fruits, sweets)",
    "professions & trades",
    "family, wedding & guests (hospitality culture)",
    "character traits & emotions",
    "famous Armenians, past & present",
    "world-famous people",
    "everyday actions (verbs)",
    "yard & neighborhood life",
    "market, shopping & money",
    "transport & road life",
    "cartoons & fairy tales",
    "films & TV",
    "music & dance",
    "childhood games & school",
    "Soviet-era nostalgia items",
    "holidays & traditions",
    "superstitions & folk beliefs",
    "animals & nature",
    "geography & landmarks",
    "technology & internet life",
    "sports & games you play (chess, backgammon, arm wrestling)",
    "army & service",
    "bureaucracy & queues",
    "renovation & the varpet",
    "village & farm life",
    "weather & seasons",
    "feast & toasting culture (tamada, toasts)",
    "travel & airport",
    "buildings & city life (elevator, balcony, basement)",
    "tools & workshop",
    "space & sky",
    "church & rituals",
    "numbers & counting",
    "time & calendar",
    "simple abstract concepts (beginning, flight, secret)",
    "qualities & sensations (adjectives)",
    "shapes & measurement",
    "materials (iron, glass, wood)",
]


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


def pick_show_set(candidates: list[dict]) -> list[dict]:
    """Randomly sample the show set from the candidate pool. Trend picks are
    kept (they're the timely part, capped at MAX_TREND_WORDS anyway);
    wildcards are drawn by lot so consecutive runs diverge even when the
    model's pool barely changes.

    The draw is domain-aware: at most one word per model-tagged domain, so
    a domain the model over-filled (4 famous Armenians...) can't dominate
    the show. A second pass relaxes the cap only if the pool is too narrow
    to fill the set otherwise."""
    trends = [c for c in candidates if c.get("source_topic") != "wildcard"]
    trends = trends[:MAX_TREND_WORDS]
    wildcards = [c for c in candidates if c.get("source_topic") == "wildcard"]

    used_domains = {c.get("domain", "").lower() for c in trends}
    chosen = list(trends)
    for c in random.sample(wildcards, len(wildcards)):  # shuffled copy
        if len(chosen) == WORD_COUNT:
            break
        domain = c.get("domain", "").lower()
        if domain and domain in used_domains:
            continue
        used_domains.add(domain)
        chosen.append(c)
    for c in random.sample(wildcards, len(wildcards)):  # relax cap if short
        if len(chosen) == WORD_COUNT:
            break
        if c not in chosen:
            chosen.append(c)

    random.shuffle(chosen)
    return chosen


def main():
    topics = fetch_all()
    if not topics:
        raise SystemExit("No topics fetched from any geo — check the network.")

    print(f"Fetched {len(topics)} trending topics:")
    for topic in topics:
        print(f"  - {topic}")

    orch = Orchestrator()
    if orch.config.get("rehearsal_mode"):
        avoid = []
        print("\nREHEARSAL MODE (config.yaml rehearsal_mode: true): recent-word "
              "blocking is OFF — set it to false before the first real show.")
    else:
        avoid = recent_words()
        if avoid:
            print(f"\nAvoiding {len(avoid)} recently used words: {', '.join(avoid)}")

    focus = random.sample(DOMAINS, FOCUS_DOMAINS_PER_RUN)
    print(f"\nFocus domains this run: {', '.join(focus)}")

    # persist=False: last_output must hold the sampled SHOW set (it feeds
    # recent_words and generate_hints), not the raw candidate pool.
    result = orch.run(
        "trend_researcher",
        "Build a broad candidate pool: mostly wildcard words of your own "
        "invention, seasoned with at most a few strong picks from these "
        "trending topics.",
        context={
            "topics": topics,
            "recent_words": avoid,
            "count": CANDIDATE_COUNT,
            "max_trend_words": MAX_TREND_WORDS,
            "focus_domains": focus,
        },
        persist=False,
    )

    candidates = result.get("words", [])
    print(f"\nCandidate pool ({len(candidates)}): "
          f"{', '.join(c['word'] for c in candidates)}")

    show_set = {"words": pick_show_set(candidates)}
    db.save_state("trend_researcher", "last_output", show_set)

    print("\nSelected words (randomly sampled from the pool):")
    print(json.dumps(show_set, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
