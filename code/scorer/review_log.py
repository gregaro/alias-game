"""Read a show log and answer the two questions worth acting on.

    python code/scorer/review_log.py                      # newest log
    python code/scorer/review_log.py <path/to/log.jsonl>

1. MISSING ANSWER SPELLINGS. Every message that didn't match is logged with the
   word that was open at the time. Group them and the near-misses jump out: five
   people typing «բուլղարականը» while պղպեղ is open is not chatter, it is a
   spelling missing from answers[]. Matching is exact, so each one is a viewer
   who knew the word and scored zero — and nothing else in the system can see
   them. Add the good ones to questions.json.

2. PACING. When people solved (off the teaser, or only once the long hint
   landed) and who missed the window entirely. If answers cluster just past the
   close, the fix is more silence after the hint in the RENDER — never a longer
   window, which would end after the host has already said the word.

The log is append-only JSONL, one object per line; nothing here writes.
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from normalize import normalize

LOG_DIR = HERE.parent / "logs"


def newest_log() -> Path:
    logs = sorted(LOG_DIR.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not logs:
        raise SystemExit(f"No logs under {LOG_DIR} — run a show first.")
    return logs[-1]


def load(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main():
    p = argparse.ArgumentParser(description="Review a show log.")
    p.add_argument("log", nargs="?", help="path to a .jsonl log (default: newest)")
    p.add_argument("--min-count", type=int, default=1,
                   help="only show unmatched texts typed at least this often")
    args = p.parse_args()

    path = Path(args.log) if args.log else newest_log()
    rows = load(path)
    start = next((r for r in rows if r["event"] == "show_start"), {})
    print(f"Log:     {path}")
    print(f"Episode: {start.get('episode', '?')}  "
          f"({start.get('mode', '?')} mode, {start.get('questions', '?')} words)\n")

    chat = [r for r in rows if r["event"] == "chat"]
    if not chat:
        raise SystemExit("No chat in this log.")
    by_verdict = Counter(r["verdict"] for r in chat)
    print("Messages: " + "  ".join(f"{v}={n}" for v, n in by_verdict.most_common()))

    # ---- 1. spellings we are missing ----
    # An unmatched message sent while a word was open. Most are chatter; the ones
    # several people typed are almost always a variant we forgot.
    misses = defaultdict(Counter)
    for r in chat:
        if r["verdict"] == "no_match" and r.get("word"):
            key = normalize(r["text"])
            if key and len(key) <= 40:      # a sentence is chat, not an attempt
                misses[r["word"]][r["text"]] += 1

    print("\n=== unmatched, while a word was open ===")
    print("Anything several people typed is probably a missing spelling.\n")
    found = False
    for word, texts in misses.items():
        top = [(t, n) for t, n in texts.most_common(8) if n >= args.min_count]
        if not top:
            continue
        found = True
        print(f"  {word}")
        for t, n in top:
            print(f"      {n:>2}x  {t}")
    if not found:
        print("  (nothing — every attempt matched)")

    # ---- 2. pacing ----
    scored = [r for r in chat if r["verdict"] == "scored"]
    late = [r for r in chat if r["verdict"] == "late"]
    on_teaser = [r for r in scored if r.get("after_hint", 0) < 0]
    on_hint = [r for r in scored if r.get("after_hint", -1) >= 0]

    print("\n=== pacing ===")
    print(f"Solved on the teaser: {len(on_teaser)}   "
          f"after the long hint: {len(on_hint)}")
    if on_hint:
        secs = sorted(r["after_hint"] for r in on_hint)
        print(f"  they took {secs[0]:.0f}-{secs[-1]:.0f}s after the hint "
              f"(median {secs[len(secs) // 2]:.0f}s)")

    if not late:
        print("\nNobody was too late — the silence after the hint is long enough.")
    else:
        print(f"\n{len(late)} correct answer(s) arrived after the window shut:")
        for r in sorted(late, key=lambda r: -r["seconds_late"]):
            print(f"  {r['seconds_late']:>4.1f}s late  {r['name']:<20} «{r['word']}»")
        worst = max(r["seconds_late"] for r in late)
        print(f"\nAdd ~{worst + 2:.0f}s more silence after the long hint in the "
              f"render.\nDo NOT widen the window — it closes where the host says "
              "the answer.")

    # ---- per-word difficulty, for the hint writer ----
    per_word = defaultdict(int)
    for r in scored:
        per_word[r["word"]] += 1
    words = start.get("words") or list(per_word)
    print("\n=== solves per word ===")
    for w in words:
        n = per_word.get(w, 0)
        print(f"  {n:>3}  {'█' * n}{'  (nobody got it)' if not n else ''}  {w}")


if __name__ == "__main__":
    main()
