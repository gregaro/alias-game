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
import json
import os
import re
from pathlib import Path

import db

HERE = Path(__file__).parent
QUESTIONS_FILE = HERE.parent / "questions" / "questions.json"

DEFAULTS = {"window_seconds": 25, "points": [10, 8, 7, 6, 5, 4, 3, 2],
            "min_points": 1}


def answer_variants(word: str) -> list[str]:
    """The word plus split/joined forms for hyphenated or multi-word answers."""
    parts = re.split(r"[-\s]+", word.strip())
    variants = [word.strip(), " ".join(parts), "".join(parts)]
    return list(dict.fromkeys(variants))  # dedupe, keep order


def main():
    show = db.latest_state("hint_generator", "last_hints")
    if not show:
        raise SystemExit("No hints in the DB — run generate_hints.py first.")

    config = dict(DEFAULTS)
    if QUESTIONS_FILE.exists():
        with open(QUESTIONS_FILE, encoding="utf-8") as f:
            old = json.load(f)
        config = {k: old.get(k, v) for k, v in DEFAULTS.items()}

    config["questions"] = [
        {
            "number": i,
            # The overlay lower-third is one line; the avatar reads the same
            # hints aloud, so a compact joined form is enough on screen.
            "text": "  •  ".join(entry["hints"]),
            "answers": answer_variants(entry["word"]),
            "word": entry["word"],          # extra fields are ignored by the
            "hints": entry["hints"],        # scorer; kept for TTS/clip tooling
        }
        for i, entry in enumerate(show, 1)
    ]

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
