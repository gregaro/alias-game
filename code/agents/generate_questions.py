"""Stage three of the show pipeline: hints -> ../questions/questions.json.

Pure transform, no LLM by default: reads hint_generator/last_hints from the DB and
writes the file chat_scorer.py plays from. Top-level scoring settings
(window_seconds, points, min_points) are preserved from the existing
questions.json so tuning them survives regeneration.

Answers get mechanical variants only (hyphen/space/joined forms — the
normalizer turns "-" into a space, so Սարդ-մարդ and Սարդամարդ match
differently). Transliterations and alternate names (spiderman, ծույլ for
ալարկոտ) still need a human pass — the script prints a reminder for
words that likely need it. `--propose` asks the answer_proposer agent for
those variants and has you approve each one before it is written.
"""
import argparse
import itertools
import json
import os
import re
import sys
from pathlib import Path

import db

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))        # code/, for episode.py
import episode
sys.path.insert(0, str(HERE.parent / "scorer"))   # code/scorer/, for normalize.py
from normalize import normalize, matches, edit_distance, FUZZY_MIN_LENGTH, FUZZY_MAX_DISTANCE
from new_episode import TIMELINE_TEMPLATE

DEFAULTS = {"window_seconds": 25, "points": [10, 8, 7, 6, 5, 4, 3, 2],
            "min_points": 1}


def curated_answers() -> dict[str, list[str]]:
    """normalize(word) -> every answer ever accepted for it, newest episode
    first. Answers are the one thing here a human edits by hand, and matching is
    exact — losing them means a viewer who knew the answer scores zero. Scanning
    all episodes (not just this one) means a word that comes back around keeps
    its transliterations instead of starting from scratch.

    Keyed on the NORMALIZED word and unioned, not first-wins on the raw string:
    ep1 stored «վարսավիր» and ep6 «Վարսավիր», the exact lookup missed, and the
    word came back with none of its variants."""
    kept: dict[str, list[str]] = {}
    for name in reversed(episode.list_episodes()):      # newest first
        f = episode.EPISODES_DIR / name / episode.QUESTIONS
        if not f.is_file():
            continue
        for q in json.load(open(f, encoding="utf-8")).get("questions", []):
            if q.get("word") and q.get("answers"):
                kept.setdefault(normalize(q["word"]), []).extend(q["answers"])
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


def _clashes_with_other_word(variant: str, word: str, questions: list[dict]) -> str | None:
    """The OTHER word this variant would blur into, if any — same test as
    check_collisions, applied per variant so one bad proposal is dropped
    instead of aborting the whole episode."""
    nv = normalize(variant)
    for q in questions:
        if q["word"] == word:
            continue
        for a in q["answers"]:
            na = normalize(a)
            if nv == na or (len(nv) >= FUZZY_MIN_LENGTH and len(na) >= FUZZY_MIN_LENGTH
                            and edit_distance(nv, na) <= FUZZY_MAX_DISTANCE):
                return q["word"]
    return None


def propose_variants(questions: list[dict], ep_name: str | None) -> dict[str, list[str]]:
    """Ask the answer_proposer agent for extra spellings; keep only the ones
    worth a human's glance. Never trusts the model: a variant the scorer would
    already accept is noise, and one that lands on another word's answer is
    dropped here, in code, whatever the model thought."""
    from dotenv import load_dotenv
    load_dotenv()                      # API keys, before any model is built
    from orchestrator import Orchestrator
    orch = Orchestrator()
    payload = [{"word": q["word"], "hints": q["hints"], "accepted": q["answers"]}
               for q in questions]
    result = orch.run("answer_proposer",
                      "Propose new accepted answers for these words.",
                      {"words": payload}, persist=False)
    by_word = {e.get("word"): e.get("variants") or [] for e in result.get("words", [])}

    proposals: dict[str, list[str]] = {}
    for q in questions:
        keep = []
        for v in by_word.get(q["word"], []):
            v = str(v).strip()
            if not v or matches(v, q["answers"] + keep):
                continue               # already accepted (or a forgiven typo)
            clash = _clashes_with_other_word(v, q["word"], questions)
            if clash:
                print(f"  dropped {v!r} for {q['word']!r}: collides with {clash!r}")
                continue
            keep.append(v)
        if keep:
            proposals[q["word"]] = keep
    return proposals


def review_proposals(questions: list[dict], proposals: dict[str, list[str]]) -> int:
    """Approve or reject each proposal at the terminal. Per word: Enter keeps
    all, `n` rejects all, or type the numbers to DROP (e.g. `2 5`). Without a
    terminal nothing is merged — a pipe must never approve on your behalf."""
    if not sys.stdin.isatty():
        print("No terminal: proposals NOT merged. Re-run --propose interactively.")
        return 0
    added = 0
    for q in questions:
        props = proposals.get(q["word"])
        if not props:
            continue
        print(f"\n{q['number']}. {q['word']}   (has: {', '.join(q['answers'])})")
        for i, v in enumerate(props, 1):
            print(f"   {i}. {v}")
        reply = input("   Enter = keep all, n = none, or numbers to drop: ").strip().lower()
        if reply == "n":
            continue
        drop = {int(t) for t in reply.replace(",", " ").split() if t.isdigit()}
        for i, v in enumerate(props, 1):
            if i not in drop:
                q["answers"].append(v)
                added += 1
    return added


def check_collisions(questions: list[dict]) -> None:
    """Refuse to write an episode where two DIFFERENT words could score off
    the same chat message. scorer.matches() now accepts one-character typos
    on answers >= FUZZY_MIN_LENGTH chars (see normalize.py), so it is no
    longer enough to check exact strings — two words' answers could also be
    close enough in edit distance to blur into each other. Checked against
    all three existing episodes when this landed: zero collisions, either
    way. Better to fail loudly here than have the scorer credit the wrong
    word live."""
    entries = [(q["word"], normalize(a)) for q in questions for a in q["answers"]]

    # Exact: literally the same normalized string under two different words.
    owner = {}
    for word, na in entries:
        if na in owner and owner[na] != word:
            raise SystemExit(f"Answer collision: {na!r} matches BOTH "
                             f"{owner[na]!r} and {word!r} — fix answers[] "
                             "for one of them before running the show.")
        owner[na] = word

    # Fuzzy: close enough in edit distance that the SAME typo the scorer now
    # forgives on one word would also land inside another word's tolerance.
    long_entries = [(w, na) for w, na in entries if len(na) >= FUZZY_MIN_LENGTH]
    for (w1, a1), (w2, a2) in itertools.combinations(long_entries, 2):
        if w1 != w2 and edit_distance(a1, a2) <= FUZZY_MAX_DISTANCE:
            raise SystemExit(
                f"Fuzzy answer collision: {w1!r}'s answer {a1!r} is only "
                f"{FUZZY_MAX_DISTANCE} edit(s) from {w2!r}'s answer {a2!r} — "
                "a chat typo could score the wrong word. Fix one of them "
                "before running the show.")


_WINDOW_ROW_RE = re.compile(
    r'\{\s*"word":\s*("(?:[^"\\]|\\.)*"),\s*'
    r'"start":\s*(null|"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?),\s*'
    r'"teaser":\s*(null|"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?),\s*'
    r'"hint":\s*(null|"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?)\s*\}'
)


def _compact_windows(text: str) -> str:
    """Collapse each {word, start, teaser, hint} row back onto one line.

    json.dumps's indent=2 blows every row across 6 lines, but every hand-
    measured timeline.json in this repo uses one row per line — much easier
    to scan while scrubbing through 10 rows of second-marks. Regex, not a
    custom encoder, because only the WINDOW rows need this; the _comment
    block and outro_start/outro_end should stay in normal indented JSON."""
    return _WINDOW_ROW_RE.sub(r'{"word": \1, "start": \2, "teaser": \3, "hint": \4}', text)


def sync_timeline(words: list[str], ep_name: str | None = None) -> None:
    """Keep timeline.json's row list in step with the word order.

    new_episode.py's template has always SAID "run generate_questions.py
    first: it fills in the word list here" — but nothing ever actually did
    that, so every episode's timeline.json sat at "windows": [] until you
    typed out all 10 rows by hand before you could even start measuring. This
    is stage 3 finally doing what its own template claimed, since stage 3 is
    the first point in the pipeline where the final word list is known —
    new_episode.py (stage 0) runs before the words are even researched.

    Idempotent and non-destructive: a row already carrying a measured mark
    for that same word is left untouched. Only rows for a NEW or reordered
    word get reset to an empty, unmeasured template. So re-running stage 3
    after tweaking hints (same words) never erases marks you already spent
    time scrubbing off the video."""
    path = episode.path(episode.TIMELINE, ep_name)
    if path.exists():
        data = json.load(open(path, encoding="utf-8"))
    else:
        data = dict(TIMELINE_TEMPLATE)   # fallback; new_episode.py normally
                                         # already created this file in stage 0

    old_rows = {row.get("word"): row for row in data.get("windows") or []}
    new_rows, reused = [], 0
    for word in words:
        old = old_rows.get(word)
        if old and any(old.get(k) is not None for k in ("start", "teaser", "hint")):
            new_rows.append(old)
            reused += 1
        else:
            new_rows.append({"word": word, "start": None, "teaser": None, "hint": None})
    data["windows"] = new_rows
    data.setdefault("outro_start", None)
    data.setdefault("outro_end", None)

    text = _compact_windows(json.dumps(data, ensure_ascii=False, indent=2)) + "\n"
    tmp = str(path) + ".tmp"
    open(tmp, "w", encoding="utf-8").write(text)
    os.replace(tmp, path)

    fresh = len(new_rows) - reused
    print(f"Synced {path.name}: {len(new_rows)} row(s)"
          + (f" ({reused} measured mark(s) preserved, {fresh} fresh)" if reused else "")
          + ".")


def main():
    parser = argparse.ArgumentParser(description="Stage 3: hints -> questions.json")
    episode.add_argument(parser)
    parser.add_argument("--propose", action="store_true",
                        help="ask the answer_proposer agent for extra spellings "
                             "and approve them one by one (one LLM call)")
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
                                          + kept.get(normalize(entry["word"]), []))),
            "word": entry["word"],          # extra fields are ignored by the
            "hints": entry["hints"],        # scorer; kept for TTS/clip tooling
        }
        for i, entry in enumerate(show, 1)
    ]
    carried = sum(1 for e in show if normalize(e["word"]) in kept)
    if carried:
        print(f"Carried hand-added answers forward for {carried} word(s).\n")

    if args.propose:
        print("Asking answer_proposer for variants...")
        proposals = propose_variants(config["questions"], args.episode)
        if proposals:
            n = review_proposals(config["questions"], proposals)
            print(f"\nApproved {n} proposed variant(s).\n")
        else:
            print("No new variants proposed.\n")

    check_collisions(config["questions"])   # never write a file that could
                                            # score the wrong word live

    tmp = str(QUESTIONS_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, QUESTIONS_FILE)

    print(f"Wrote {len(config['questions'])} questions to {QUESTIONS_FILE}\n")
    for q in config["questions"]:
        print(f"  {q['number']}. {q['word']}  ->  answers: {q['answers']}")

    sync_timeline([e["word"] for e in show], args.episode)

    print("\nBEFORE THE SHOW: hand-add variants viewers will actually type —")
    print("transliterations for names (spiderman, tsarukyan), synonyms the")
    print("hints point at, common misspellings. The scorer matches EXACTLY.")
    if not args.propose:
        print("(--propose drafts these with an LLM; you approve each one.)")


if __name__ == "__main__":
    main()
