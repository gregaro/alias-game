"""Stage three of the show pipeline: hints -> ../questions/questions.json.

Pure transform, no LLM: reads hint_generator/last_hints from the DB and
writes the file chat_scorer.py plays from. Top-level scoring settings
(window_seconds, points, min_points) are preserved from the existing
questions.json so tuning them survives regeneration.

Answers get mechanical variants only (hyphen/space/joined forms — the
normalizer turns "-" into a space, so Սարդ-մարդ and Սարդամարդ match
differently). Transliterations and alternate names (spiderman, ծույլ for
ալարկոտ) still need a human pass — the script prints a reminder for
words that likely need it.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

import db

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))        # code/, for episode.py
import episode

DEFAULTS = {"window_seconds": 25, "points": [10, 8, 7, 6, 5, 4, 3, 2],
            "min_points": 1}


def curated_answers() -> dict[str, list[str]]:
    """word -> hand-curated answers, gathered from EVERY past episode, newest
    first. Answers are the one thing here a human edits by hand, and matching is
    exact — losing them means a viewer who knew the answer scores zero. Scanning
    all episodes (not just this one) means a word that comes back around keeps
    its transliterations instead of starting from scratch."""
    kept: dict[str, list[str]] = {}
    for name in reversed(episode.list_episodes()):      # newest first
        f = episode.EPISODES_DIR / name / episode.QUESTIONS
        if not f.is_file():
            continue
        for q in json.load(open(f, encoding="utf-8")).get("questions", []):
            if q.get("word") and q.get("answers"):
                kept.setdefault(q["word"], q["answers"])
    return kept


def answer_variants(word: str) -> list[str]:
    """The word plus the joined form, for hyphenated or multi-word answers.

    Only the JOINED form is worth listing. The normalizer turns "-" into a
    space, so "Սարդ-մարդ" and "Սարդ մարդ" already compare equal — a
    space-separated variant would never match anything the original doesn't.
    "Սարդմարդ" is a genuinely different string, so it does need listing."""
    joined = "".join(re.split(r"[-\s]+", word.strip()))
    variants = [word.strip(), joined]
    return list(dict.fromkeys(variants))  # dedupe, keep order


def main():
    parser = argparse.ArgumentParser(description="Stage 3: hints -> questions.json")
    episode.add_argument(parser)
    args = parser.parse_args()
    QUESTIONS_FILE = episode.path(episode.QUESTIONS, args.episode)
    print(f"Episode: {episode.resolve_name(args.episode)}\n")

    show = db.latest_state("hint_generator", "last_hints")
    if not show:
        raise SystemExit("No hints in the DB — run generate_hints.py first.")

    config = dict(DEFAULTS)
    if QUESTIONS_FILE.exists():
        with open(QUESTIONS_FILE, encoding="utf-8") as f:
            old = json.load(f)
        config = {k: old.get(k, v) for k, v in DEFAULTS.items()}
    kept = curated_answers()

    config["questions"] = [
        {
            "number": i,
            # The overlay lower-third is one line; the avatar reads the same
            # hints aloud, so a compact joined form is enough on screen.
            "text": "  •  ".join(entry["hints"]),
            # Union, order-preserving: the mechanical variants plus whatever was
            # hand-added for this word last time.
            "answers": list(dict.fromkeys(answer_variants(entry["word"])
                                          + kept.get(entry["word"], []))),
            "word": entry["word"],          # extra fields are ignored by the
            "hints": entry["hints"],        # scorer; kept for TTS/clip tooling
        }
        for i, entry in enumerate(show, 1)
    ]
    carried = sum(1 for e in show if e["word"] in kept)
    if carried:
        print(f"Carried hand-added answers forward for {carried} word(s).\n")

    tmp = str(QUESTIONS_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, QUESTIONS_FILE)

    print(f"Wrote {len(config['questions'])} questions to {QUESTIONS_FILE}\n")
    for q in config["questions"]:
        print(f"  {q['number']}. {q['word']}  ->  answers: {q['answers']}")
    print("\nBEFORE THE SHOW: hand-add variants viewers will actually type —")
    print("transliterations for names (spiderman, tsarukyan), synonyms the")
    print("hints point at, common misspellings. The scorer matches EXACTLY.")


if __name__ == "__main__":
    main()
