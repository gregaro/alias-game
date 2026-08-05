"""Read an agent-run trace and print the things worth acting on.

    python code/agents/review_agents.py                    # newest run
    python code/agents/review_agents.py <path/to/run.jsonl>
    python code/agents/review_agents.py --prompts <word>   # rendered prompts

The scorer has review_log.py for shows; this is the same idea for agent
runs, and for the same reason. A one-shot script fails visibly. A loop
fails invisibly — so these are the four questions a loop can't answer by
itself:

1. WHAT DIDN'T REACH THE BAR. Every word that ended best_effort or
   gates_failed, with the gate or score that held it back. gates_failed
   words are in the show set right now and need a human pass before TTS.

2. WHERE THE LOOP MADE THINGS WORSE. A round that scored below the round
   before it means the revise instructions backfired — the writer took a
   note badly, or "fix hint 1" collateral-damaged hint 2. This is the
   single most useful signal in the file and nothing else surfaces it.

3. WHAT THE SOLVER DID. Words the blind solver got off the teaser (too
   easy) or never got at all (too obscure). Treat with care: gpt-4o-mini
   is not your audience — it will crush a Marvel reference and whiff on a
   Yerevan neighbourhood joke, which is exactly backwards from the people
   watching. Compare against the real solve rates in code/logs/<episode>/
   after a show before trusting it.

4. WHAT IT COST. Tokens per agent and model, so a run that quietly spent
   40 calls on one stubborn word is visible before the invoice is.

Append-only JSONL, one object per line; nothing here writes.
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / "logs" / "agents"

# Cost per 1M tokens, if you want money instead of tokens. Left empty on
# purpose: prices move, and a stale number quietly reported as fact is worse
# than no number. Fill in what you actually pay, e.g.
#   "google/gemini-3.1-pro-preview": (1.25, 10.00),   # (input, output)
PRICES: dict[str, tuple[float, float]] = {}


def newest_log() -> Path:
    logs = sorted(LOG_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not logs:
        raise SystemExit(f"No agent traces under {LOG_DIR} — run a stage first.")
    return logs[-1]


def load(path: Path) -> list[dict]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass          # a run killed mid-write leaves one torn line
    return events


SOLVER_RANK = {3: 2, 1: 1, None: 0}     # mirrors HintSpec._SOLVER_RANK


def rank(ev: dict) -> tuple:
    """Same ranking hint_loop.py uses, recomputed from the log — including
    the solver term, so a round that started giving the answer away in the
    teaser reads as the regression it is, not as an improvement."""
    gates = ev.get("gates") or {}
    scores = (ev.get("scores") or {}).values()
    clean = bool(gates) and all(v == "pass" for v in gates.values())
    solved = (ev.get("extra") or {}).get("solved_at")
    return (clean, SOLVER_RANK.get(solved, 0),
            min(scores) if scores else 0, sum(scores))


def report_outcomes(events: list[dict]) -> None:
    ends = [e for e in events if e["event"] == "target_end"]
    if not ends:
        print("No completed targets in this trace.\n")
        return

    by_status = defaultdict(list)
    for e in ends:
        by_status[e["status"]].append(e)

    print("=" * 72)
    print("OUTCOMES")
    print("=" * 72)
    for status in ("accepted", "best_effort", "gates_failed"):
        got = by_status.get(status, [])
        print(f"  {status:<13} {len(got)}")
    print()

    needs = by_status.get("gates_failed", []) + by_status.get("best_effort", [])
    if not needs:
        print("Every word reached the bar.\n")
        return

    print("Did not reach the bar — read these before recording:")
    for e in needs:
        # The BEST round, not the last one: that is the candidate that
        # actually got kept, so it is the one whose weaknesses you inherit.
        best = next((r for r in events
                     if r["event"] == "round" and r.get("target") == e["target"]
                     and r.get("round") == e.get("best_round")), {})
        failed = {k: v for k, v in (best.get("gates") or {}).items() if v != "pass"}
        detail = "; ".join(f"{k}: {v}" for k, v in failed.items())
        if not detail:
            scores = best.get("scores") or {}
            weak = [f"{k} {v}" for k, v in sorted(scores.items()) if v < 4]
            solved = (best.get("extra") or {}).get("solved_at")
            detail = ", ".join(weak)
            if solved != 3:
                note = "solved off the teaser" if solved == 1 else "solver never got it"
                detail = f"{detail}; {note}" if detail else note
        print(f"  {e['status']:<13} {e['target']:<20} (round {e.get('best_round')}) "
              f"{detail}")
    print()


def report_regressions(events: list[dict]) -> None:
    rounds = defaultdict(list)
    for e in events:
        if e["event"] == "round":
            rounds[e["target"]].append(e)

    worse = []
    for target, rs in rounds.items():
        for prev, cur in zip(rs, rs[1:]):
            if rank(cur) < rank(prev):
                worse.append((target, prev, cur))

    print("=" * 72)
    print("REGRESSIONS — the revise round made it worse")
    print("=" * 72)
    if not worse:
        print("None. Every revise round held or improved.\n")
        return
    for target, prev, cur in worse:
        print(f"  {target}  round {prev['round']} -> {cur['round']}")
        print(f"    was: {prev.get('scores')}  gates_clean={rank(prev)[0]}  "
              f"solved_at={(prev.get('extra') or {}).get('solved_at')}")
        print(f"    now: {cur.get('scores')}  gates_clean={rank(cur)[0]}  "
              f"solved_at={(cur.get('extra') or {}).get('solved_at')}")
        notes = (prev.get("extra") or {}).get("notes") or {}
        if notes:
            print(f"    the note it was given: {json.dumps(notes, ensure_ascii=False)}")
    print("\n  Read the rendered revise prompt for one of these:")
    print(f"    python code/agents/review_agents.py --prompts {worse[0][0]}\n")


def report_solver(events: list[dict]) -> None:
    # One line per WORD, judged on its kept round — not one line per round,
    # which just repeats the same complaint three times.
    best_round = {e["target"]: e.get("best_round")
                  for e in events if e["event"] == "target_end"}
    easy, missed = [], []
    for e in events:
        if e["event"] != "round" or e["round"] != best_round.get(e["target"]):
            continue
        solved = (e.get("extra") or {}).get("solved_at")
        if solved == 1:
            easy.append((e["target"], e["round"]))
        elif solved is None and e.get("scores"):
            missed.append((e["target"], e["round"]))

    print("=" * 72)
    print("BLIND SOLVER")
    print("=" * 72)
    if easy:
        print("  Solved on the TEASER (hint 1 gives too much away):")
        for target, rnd in easy:
            print(f"    {target}  (round {rnd})")
    if missed:
        print("  NEVER solved, even with hints 1 + 3 (may be too obscure —")
        print("  or may just be too Armenian for gpt-4o-mini; judge yourself):")
        for target, rnd in missed:
            print(f"    {target}  (round {rnd})")
    if not easy and not missed:
        print("  Every round landed on hint 3, as intended.")
    print()


def report_cost(events: list[dict]) -> None:
    per = defaultdict(lambda: {"calls": 0, "in": 0, "out": 0, "ms": 0})
    for e in events:
        if e["event"] != "llm_call":
            continue
        row = per[(e.get("agent"), e.get("model"))]
        row["calls"] += 1
        row["in"] += e.get("tokens_in") or 0
        row["out"] += e.get("tokens_out") or 0
        row["ms"] += e.get("latency_ms") or 0

    print("=" * 72)
    print("COST")
    print("=" * 72)
    if not per:
        print("  No LLM calls recorded.\n")
        return
    print(f"  {'agent':<18}{'model':<34}{'calls':>6}{'tok in':>10}{'tok out':>9}")
    total_calls = 0
    total_cost = 0.0
    for (agent, model), row in sorted(per.items()):
        total_calls += row["calls"]
        print(f"  {str(agent):<18}{str(model):<34}{row['calls']:>6}"
              f"{row['in']:>10}{row['out']:>9}")
        if model in PRICES:
            pin, pout = PRICES[model]
            total_cost += row["in"] / 1e6 * pin + row["out"] / 1e6 * pout
    print(f"\n  {total_calls} calls total.")
    if total_cost:
        print(f"  Approx cost: ${total_cost:.2f}  (from the PRICES table)")
    else:
        print("  Fill in PRICES at the top of this file to see money.")
    print()


def show_prompts(events: list[dict], target: str) -> None:
    calls = [e for e in events
             if e["event"] == "llm_call" and e.get("target") == target]
    if not calls:
        raise SystemExit(f"No calls logged for {target!r} in this trace.")
    for e in calls:
        print("=" * 72)
        print(f"round {e.get('round')}  {e.get('role')}  "
              f"[{e.get('agent')} / {e.get('model')}]  "
              f"{e.get('latency_ms')}ms")
        print("=" * 72)
        print("--- PROMPT (as rendered) ---")
        print(e.get("prompt", ""))
        print("\n--- RESPONSE ---")
        print(e.get("response", ""))
        print()


def main():
    p = argparse.ArgumentParser(description="Review an agent run trace.")
    p.add_argument("log", nargs="?", help="path to a .jsonl trace (default: newest)")
    p.add_argument("--prompts", metavar="WORD",
                   help="dump every rendered prompt and response for one word")
    args = p.parse_args()

    path = Path(args.log) if args.log else newest_log()
    events = load(path)
    print(f"\nTrace: {path}  ({len(events)} events)\n")

    if args.prompts:
        show_prompts(events, args.prompts)
        return

    report_outcomes(events)
    report_regressions(events)
    report_solver(events)
    report_cost(events)


if __name__ == "__main__":
    main()
