"""Model prices (USD per 1M tokens) kept in prices.json, with a staleness check.

Prices drift, and a stale number reported as fact is worse than none — so every
model carries its own `checked` date, and anything older than STALE_DAYS is
flagged wherever prices are used (review_agents.py, new_episode.py).

    python code/agents/prices.py             # show prices + what is stale
    python code/agents/prices.py --refresh   # re-pull OpenRouter models

--refresh covers OpenRouter slugs only (public API, no key). Anthropic has no
pricing API: edit those entries in prices.json by hand and bump `checked`.
A model whose refresh fails keeps its old date, so it stays flagged.
"""
import argparse
import json
import urllib.request
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
PRICES_FILE = HERE / "prices.json"
STALE_DAYS = 30
OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"


def load() -> dict:
    if not PRICES_FILE.is_file():
        return {"models": {}}
    return json.loads(PRICES_FILE.read_text(encoding="utf-8"))


def table() -> dict[str, tuple[float, float]]:
    """{model: (in, out)} — the shape review_agents.py wants."""
    return {m: (v["in"], v["out"]) for m, v in load()["models"].items()}


def stale(today: date | None = None) -> list[tuple[str, int]]:
    """[(model, age_in_days)] for entries older than STALE_DAYS."""
    today = today or date.today()
    out = []
    for m, v in load()["models"].items():
        age = (today - date.fromisoformat(v["checked"])).days
        if age > STALE_DAYS:
            out.append((m, age))
    return out


def warning() -> str | None:
    """One printable line if anything is stale, else None."""
    s = stale()
    if not s:
        return None
    names = ", ".join(f"{m} ({age}d)" for m, age in s)
    return (f"Model prices are stale (> {STALE_DAYS} days): {names}. "
            f"Run `python code/agents/prices.py --refresh`; Anthropic models "
            f"need a hand edit of prices.json.")


def refresh() -> None:
    data = load()
    try:
        with urllib.request.urlopen(OPENROUTER_MODELS, timeout=20) as r:
            live = {m["id"]: m["pricing"] for m in json.load(r)["data"]}
    except Exception as e:                      # offline, DNS, 5xx...
        raise SystemExit(f"Could not reach OpenRouter ({e}); nothing changed.")

    today = date.today().isoformat()
    for model, v in data["models"].items():
        if "/" not in model:
            print(f"  {model:<34} manual — edit prices.json "
                  f"(last checked {v['checked']})")
            continue
        p = live.get(model)
        if not p:
            print(f"  {model:<34} NOT on OpenRouter any more — left as is")
            continue
        new_in, new_out = float(p["prompt"]) * 1e6, float(p["completion"]) * 1e6
        changed = (round(new_in, 4), round(new_out, 4)) != (v["in"], v["out"])
        v.update({"in": round(new_in, 4), "out": round(new_out, 4),
                  "checked": today})
        print(f"  {model:<34} ${new_in:g} / ${new_out:g}"
              f"{'   <- CHANGED' if changed else ''}")
    PRICES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description="Show or refresh model prices.")
    p.add_argument("--refresh", action="store_true",
                   help="re-pull OpenRouter model prices")
    args = p.parse_args()
    if args.refresh:
        refresh()
        print()
    for m, v in load()["models"].items():
        print(f"  {m:<34} ${v['in']:g} in / ${v['out']:g} out   "
              f"checked {v['checked']}")
    w = warning()
    print(f"\n  {w}" if w else "\n  All prices are fresh.")


if __name__ == "__main__":
    main()
