"""Stage two of the show pipeline: latest word set -> hints for every word.

This stage is a real agent loop, not a single API call. Per word:

    generate -> code gates -> blind solver -> critic -> revise -> ...

up to `refinement.max_rounds` rounds (see config.yaml), stopping early on
acceptance or on a plateau. Three vendors are involved so that nothing
grades its own work — writer (Gemini), critic (Claude), blind solver
(gpt-4o-mini). The loop itself is refine.py; what "good" means is
hint_loop.py.

Every word ends in one of four states, and the point of the loop is that
the bad ones say so out loud instead of quietly entering the show:

    accepted     hit the bar: gates clean, every score >= 4, and the blind
                 solver reached the word on hint 3 and not before
    best_effort  gates clean but scores or solver fell short — usable,
                 worth a glance
    gates_failed never produced a candidate that passed the hard gates —
                 goes into the show set AND into REVIEW NEEDED
    error        every round threw; the word is dropped, 9 words still
                 make a show

Words refine in parallel (`refinement.parallel_words`) because they are
independent and the loop is otherwise slow enough to be annoying.

Hints go into avatar TTS, so read the printed output before recording.
Full round-by-round detail lands in code/logs/agents/<timestamp>.jsonl —
read it back with review_agents.py.
"""
import argparse
import sys
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

load_dotenv()  # load API keys before building any model

import db
from hint_loop import HintSpec
from orchestrator import Orchestrator
from refine import refine
from tracing import Trace


def known_variants() -> dict:
    """Hand-curated answers from past episodes, so the blind solver is judged
    against everything a VIEWER could type and be scored for.

    Without this the solver is held to a stricter bar than the audience: on a
    real run it answered «ծույլ» for «ալարկոտ» — a synonym CLAUDE.md lists as
    an accepted answer — and a working hint ladder was marked too obscure
    three rounds running. Stage 3 owns this list; borrowing it read-only is
    cheaper than duplicating it. Absent or unreadable episodes are not an
    error: an empty list just makes the solver strict again.
    """
    try:
        from generate_questions import curated_answers
        return curated_answers()
    except Exception as exc:
        print(f"note: no curated answers available ({exc}); the solver will "
              f"only accept the exact word.", file=sys.stderr)
        return {}


def _refine_one(spec, entry, trace, settings):
    """One word, start to finish. Returns (entry, result, error)."""
    try:
        result = refine(
            spec, entry["word"], trace,
            max_rounds=settings.get("max_rounds", 3),
            plateau_rounds=settings.get("plateau_rounds", 2),
        )
        return entry, result, None
    except Exception as exc:
        return entry, None, exc


def dry_run(word: str) -> None:
    """One word, printed, nothing saved. Still writes a trace."""
    orch = Orchestrator()
    settings = orch.settings("refinement")
    spec = HintSpec(orch, settings, known_variants())
    with Trace("dryrun") as trace:
        result = refine(spec, word, trace,
                        max_rounds=settings.get("max_rounds", 3),
                        plateau_rounds=settings.get("plateau_rounds", 2))
        log_path = trace.path

    print(f"\n{word}")
    print(f"  {HintSpec.summary(result)}\n")
    for rnd in result.history:
        mark = " <- kept" if rnd.round == result.best.round else ""
        print(f"  round {rnd.round}: {rnd.scores or rnd.failed_gates}"
              f"  solved_at={rnd.extra.get('solved_at')}{mark}")
        for i, hint in enumerate(rnd.payload["hints"], 1):
            print(f"    {i}. {hint}")
        if rnd.extra.get("notes"):
            print(f"    critic: {rnd.extra['notes']}")
        print()
    print(f"Trace: {log_path}")


def main():
    p = argparse.ArgumentParser(description="Generate hints for the latest word set.")
    p.add_argument("--word", metavar="W",
                   help="refine ONE word and print the result without touching "
                        "the DB — the cheap way to sanity-check the loop or a "
                        "prompt change (~4 calls instead of ~40)")
    args = p.parse_args()

    if args.word:
        return dry_run(args.word)

    word_set = db.latest_state("trend_researcher", "last_output")
    if not word_set or not word_set.get("words"):
        raise SystemExit("No word set in the DB — run research_words.py first.")

    orch = Orchestrator()
    settings = orch.settings("refinement")
    spec = HintSpec(orch, settings, known_variants())
    entries = word_set["words"]
    workers = max(1, int(settings.get("parallel_words", 4)))

    print(f"Refining {len(entries)} words, up to "
          f"{settings.get('max_rounds', 3)} rounds each, {workers} at a time.\n")

    with Trace("hints") as trace:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # map() keeps the original word order: parallelism must not
            # reshuffle the show.
            outcomes = list(pool.map(
                lambda e: _refine_one(spec, e, trace, settings), entries))
        log_path = trace.path

    done, transcript = [], []          # done: (entry_for_db, result) pairs
    for entry, result, error in outcomes:
        if error is not None:
            print(f"warning: hints failed for {entry['word']!r}: {error}",
                  file=sys.stderr)
            continue

        done.append(({
            **entry,
            "hints": result.best.payload["hints"],
            # Carried so later stages — and you, next week — can see how hard
            # the loop had to fight for this word.
            "refinement": {
                "status": result.status,
                "round": result.best.round,
                "scores": result.best.scores,
                "solved_at": result.best.extra.get("solved_at"),
            },
        }, result))
        transcript.append({
            "word": entry["word"],
            "status": result.status,
            "rounds_used": result.rounds_used,
            "history": [
                {"round": c.round, "hints": c.payload["hints"], "gates": c.gates,
                 "scores": c.scores, "extra": c.extra}
                for c in result.history
            ],
        })

    if not done:
        raise SystemExit("Hint generation failed for every word — check the API.")

    show = [entry for entry, _ in done]
    db.save_state("hint_generator", "last_hints", show)
    db.save_state("hint_generator", "last_refinement", transcript)

    print(f"Hints for {len(show)}/{len(entries)} words "
          f"(saved to DB as hint_generator/last_hints):\n")
    for entry, result in done:
        print(f"  {entry['word']}")
        print(f"    {HintSpec.summary(result)}")
        for i, hint in enumerate(entry["hints"], 1):
            spare = "   (spare — not spoken on air)" if i == 2 else ""
            print(f"    {i}. {hint}{spare}")
        print()

    flagged = [(e["word"], r) for e, r in done if r.needs_review]
    if flagged:
        print("REVIEW NEEDED — these words did not reach the bar:")
        for word, result in flagged:
            print(f"  - {word}: {HintSpec.summary(result)}")
        print()

    accepted = len(show) - len(flagged)
    print(f"{accepted}/{len(show)} accepted outright.")
    print(f"Full trace: {log_path}")
    print("Read it back with: python code/agents/review_agents.py")


if __name__ == "__main__":
    main()
