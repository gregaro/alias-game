"""Start a new show: create its folder and point current_episode at it.

Run this ONCE when you begin planning an episode. Stages 1-4 then write into
whatever current_episode names, so re-running stage 1 because you didn't like
the words simply overwrites — it doesn't litter half-built episode folders.

    python code/agents/new_episode.py                  # ep<N+1>-<today>
    python code/agents/new_episode.py --date 2026-07-19
    python code/agents/new_episode.py --internal       # rehearsal: no public number
    python code/agents/new_episode.py --list           # what exists

Each folder gets a meta.json with its PUBLIC episode number (the next one in
line), because the ep<N> folder name also counts rehearsals. Pass --internal
for a dry run with friends and it stays null; the public counter only moves
when a show actually airs.

The new folder starts with an empty timeline.json template: the marks can only
be measured off the rendered video, and until they are the scorer safely falls
back to fixed windows rather than replaying someone else's second-marks.
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))        # code/, for episode.py
import episode

TIMELINE_TEMPLATE = {
    "_comment": [
        "Second-marks measured from the RENDERED video. 0:00 is the video's first",
        "frame — the instant you press Enter to sync the scorer.",
        "",
        "One row per word:",
        "  start   The answer window OPENS — where the host begins REVEALING THE",
        "          PREVIOUS word's answer (for word 1, where the intro ends and the",
        "          lead-in begins). Scoring for the previous word stops here.",
        "  teaser  Where the host speaks the SHORT hint; the overlay shows it then.",
        "  hint    Where the host speaks the LONG hint (~4s later); it replaces the",
        "          teaser on screen.",
        "",
        "Marks may be plain seconds (95) or clock strings (\"1:35\"), strictly",
        "increasing, with start <= teaser < hint < next row's start.",
        "",
        "The scorer NEEDS 'start' + 'outro_start'; those drive scoring. 'teaser' and",
        "'hint' only drive the overlay — leave them null and every hint just shows at",
        "window open. A bad mark is printed and ignored, never scored on.",
        "",
        "outro_end: where the host's closing line ACTUALLY FINISHES — not where the",
        "outro begins (that's outro_start). Optional: set it and the scorer",
        "auto-switches the overlay to the after-show end card (leaderboard to",
        "center, confetti, thank-you note) the instant it's reached. Leave it null",
        "and nothing happens automatically — hit /end from a browser once you see",
        "the outro wrapping up on the video.",
        "",
        "Run generate_questions.py first: it fills in the word list here.",
    ],
    "windows": [],
    "outro_start": None,
    "outro_end": None,
}


def main():
    p = argparse.ArgumentParser(description="Create a new episode folder.")
    p.add_argument("--date", metavar="YYYY-MM-DD", default=None,
                   help="episode date (default: today)")
    p.add_argument("--internal", action="store_true",
                   help="rehearsal that will never air: no public episode number")
    p.add_argument("--list", action="store_true", help="list episodes and exit")
    args = p.parse_args()

    if args.list:
        cur = episode.CURRENT_FILE.read_text().strip() \
            if episode.CURRENT_FILE.is_file() else None
        for name in episode.list_episodes():
            pub = episode.public_number(name)
            tag = f"public #{pub}" if pub else "internal"
            print(f"  {'*' if name == cur else ' '} {name:<22} {tag}")
        print("\n  (* = current)")
        return

    name = episode.next_name(args.date)
    d = episode.EPISODES_DIR / name
    if d.exists():
        raise SystemExit(f"{d} already exists — refusing to overwrite it.")
    d.mkdir(parents=True)

    with open(d / episode.TIMELINE, "w", encoding="utf-8") as f:
        json.dump(TIMELINE_TEMPLATE, f, ensure_ascii=False, indent=2)
        f.write("\n")

    pub = None if args.internal else episode.next_public_number()
    with open(d / episode.META, "w", encoding="utf-8") as f:
        json.dump({"public_episode": pub}, f, indent=2)
        f.write("\n")

    episode.set_current(name)
    print(f"Created {d}  ({'internal' if pub is None else f'public episode {pub}'})")
    print(f"current_episode -> {name}\n")
    # Cost reports use prices.json; a new episode is the natural moment to
    # notice it has gone stale, before the stages that spend money run.
    import prices
    w = prices.warning()
    if w:
        print(f"WARNING: {w}\n")
    print("Next: python code/agents/research_words.py   (stage 1)")


if __name__ == "__main__":
    main()
