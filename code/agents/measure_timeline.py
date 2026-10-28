"""measure_timeline.py — stage five (no LLM): rendered video -> timeline.json marks.

    python code/agents/measure_timeline.py VIDEO              # print proposed marks
    python code/agents/measure_timeline.py VIDEO --write      # save them
    python code/agents/measure_timeline.py VIDEO --episode ep3

Replaces scrubbing the video by hand. No speech recognition: the script is
known, and so is its SHAPE, which is enough to read the marks off the audio:

    [intro + lead-in 1 + teaser 1] pause [closer 1] guess [reveal 2 + lead-in 2
    + teaser 2] pause [closer 2] guess ... [final reveal + outro]

Every pause and every guess window is a long silence, and nothing else is as
long. So the 2N longest gaps in the audio cut the video into 2N+1 blocks, and
the blocks are named by position:
  - `hint`        = first speech after each pause           (exact)
  - `start`       = first speech after each guess window    (exact, windows 2..N)
  - `outro_start` = first speech after the last guess window (exact)
  - `outro_end`   = end of the last speech                  (exact)
  - `teaser`, and window 1's `start` (it sits inside a block, after the intro),
    are SENTENCES inside a block: read off the small gaps between sentences
    when the count matches the script, else placed by character count and
    snapped to the nearest gap (marked "est").

Prints the proposal next to whatever timeline.json already holds, so you can
see how far off it is before trusting it. --write refuses to overwrite
measured marks unless you add --force. Always eyeball the "est" and "?" rows.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))        # code/, for episode.py
import episode
from generate_questions import _compact_windows

NOISE_DB = -35      # below this counts as silence; raise if the guess windows carry a music bed
MIN_SILENCE = 0.25  # shortest gap kept as a sentence break
SNAP_SECONDS = 1.5  # an estimated sentence start snaps to a real gap only this close
MIN_SEPARATION = 1.5  # (2N-th longest gap) / (next one): below this, block cuts are a guess

# One sentence ends at . : ։ ! ? followed by space. The Armenian question mark ՞
# sits INSIDE words («Ռեա՞լը») and is not in this set on purpose.
_SENTENCE_END = re.compile(r"(?<=[.:։!?])\s+")


def sentences(text: str | None) -> list[str]:
    return [s for s in _SENTENCE_END.split((text or "").strip()) if s]


# ------------------------------- audio -------------------------------

def probe_duration(video: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", video], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def speech_segments(video: str, noise_db: float, min_silence: float):
    """[(start, end)] of audio above the silence threshold, plus total duration."""
    dur = probe_duration(video)
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", video, "-vn", "-af",
         f"silencedetect=n={noise_db}dB:d={min_silence}", "-f", "null", "-"],
        capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    silences = []
    for i, s in enumerate(starts):
        silences.append((max(s, 0.0), ends[i] if i < len(ends) else dur))
    segs, cursor = [], 0.0
    for s, e in silences:
        if s - cursor > 0.05:
            segs.append((cursor, s))
        cursor = e
    if dur - cursor > 0.05:
        segs.append((cursor, dur))
    return segs, dur


# ------------------------------ alignment ------------------------------

def cut_blocks(segs, n_cuts):
    """Split speech segments at the n_cuts longest gaps. Returns (blocks,
    separation) where separation is how clearly those gaps beat the rest."""
    gaps = [(segs[i + 1][0] - segs[i][1], i) for i in range(len(segs) - 1)]
    ranked = sorted(gaps, reverse=True)
    if len(ranked) < n_cuts:
        raise SystemExit(f"only {len(ranked)} gaps in the audio but the script needs "
                         f"{n_cuts} (a pause and a guess window per word). Is this the "
                         "right video, or is the noise threshold too high (--noise)?")
    cut_at = sorted(i for _, i in ranked[:n_cuts])
    sep = (ranked[n_cuts - 1][0] / ranked[n_cuts][0]
           if len(ranked) > n_cuts and ranked[n_cuts][0] > 0 else float("inf"))
    blocks, lo = [], 0
    for i in cut_at:
        blocks.append(segs[lo:i + 1])
        lo = i + 1
    blocks.append(segs[lo:])
    return blocks, sep


def sentence_starts(texts: list[str], segs):
    """Start time of each sentence in a block of speech segments.

    Count matches -> one segment per sentence, exact. Otherwise spread the
    sentences over the block's SPEECH time by character count, then snap each
    start to the nearest real segment start. Returns (starts, exact)."""
    if len(segs) == len(texts):
        return [s for s, _ in segs], True

    total_speech = sum(e - s for s, e in segs)
    total_chars = sum(len(t) for t in texts) or 1
    boundaries = [s for s, _ in segs]

    def wall_time(speech_pos):
        for s, e in segs:
            if speech_pos <= e - s:
                return s + speech_pos
            speech_pos -= e - s
        return segs[-1][1]

    out, chars_before = [], 0
    for j, t in enumerate(texts):
        if j == 0:
            guess = segs[0][0]
        else:
            guess = wall_time(total_speech * chars_before / total_chars)
            near = min(boundaries, key=lambda b: abs(b - guess))
            if abs(near - guess) <= SNAP_SECONDS:
                guess = near
        out.append(max(guess, out[-1] if out else 0.0))
        chars_before += len(t)
    return out, False


# ------------------------------ the script ------------------------------

def load_script(ep_name):
    return json.loads(episode.path(episode.SCRIPT_JSON, ep_name).read_text(encoding="utf-8"))


def measure(video, script, noise_db, min_silence):
    wins = script["windows"]
    n = len(wins)
    segs, dur = speech_segments(video, noise_db, min_silence)
    blocks, sep = cut_blocks(segs, 2 * n)
    notes = []

    # blocks: A1, B1, A2, B2, ..., AN, BN, FINAL
    intro = sentences(script.get("intro"))
    rows = []
    for i, w in enumerate(wins):
        a, b = blocks[2 * i], blocks[2 * i + 1]
        lead = sentences(w.get("reveal_prev")) + sentences(w.get("lead_in"))
        teaser = sentences(w.get("teaser"))
        texts = (intro if i == 0 else []) + lead + teaser
        starts, exact = sentence_starts(texts, a)
        first = len(intro) if i == 0 else 0
        start = starts[first]
        tmark = starts[len(texts) - len(teaser)]
        rows.append({
            "word": w.get("word"), "start": start, "teaser": tmark, "hint": b[0][0],
            # window 2..N starts are the block edge itself, so always exact
            "start_exact": i > 0, "teaser_exact": exact,
        })
        if len(b) != len(sentences(w.get("closer"))):
            notes.append(f"window {i + 1}: closer has {len(sentences(w.get('closer')))} "
                         f"sentence(s) but {len(b)} speech segment(s)")
    final = blocks[-1]
    return {"rows": rows, "outro_start": final[0][0], "outro_end": final[-1][1],
            "duration": dur, "notes": notes, "sep": sep, "n_cuts": 2 * n}


def check_order(m):
    """Problems that mean the marks cannot be right: the scorer needs every mark
    strictly increasing and teaser/hint inside their own window."""
    problems, rows = [], m["rows"]
    if m["sep"] < MIN_SEPARATION:
        # Wrong cuts shift EVERY later mark by a block, so this is a hard stop.
        problems.append(f"the {m['n_cuts']} longest gaps are only {m['sep']:.2f}x longer "
                        "than the next one, so pauses/guess windows can't be told from "
                        "sentence breaks (music bed in the guess windows? try --noise)")
    for i, r in enumerate(rows):
        nxt = rows[i + 1]["start"] if i + 1 < len(rows) else m["outro_start"]
        if not (r["start"] <= r["teaser"] < r["hint"] < nxt):
            problems.append(f"window {i + 1} out of order: start {r['start']:.1f} "
                            f"teaser {r['teaser']:.1f} hint {r['hint']:.1f} next {nxt:.1f}")
    if m["outro_end"] <= m["outro_start"]:
        problems.append("outro_end is not after outro_start")
    return problems


# ------------------------------ output ------------------------------

def parse_mark(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    secs = 0.0
    for part in str(v).strip().split(":"):
        secs = secs * 60 + float(part)
    return secs


def clock(t):
    return f"{int(t // 60)}:{t % 60:04.1f}"


def report(m, old):
    old_rows = (old or {}).get("windows") or []
    print(f"{'#':>2}  {'word':<14} {'start':>8} {'teaser':>8} {'hint':>8}   vs existing (delta s)")
    for i, r in enumerate(m["rows"]):
        o = old_rows[i] if i < len(old_rows) else {}
        deltas = []
        for k in ("start", "teaser", "hint"):
            ov = parse_mark(o.get(k))
            deltas.append(f"{r[k] - ov:+5.1f}" if ov is not None else "    –")
        flag = lambda ok: " " if ok else "?"
        print(f"{i + 1:>2}  {str(r['word'])[:14]:<14} "
              f"{clock(r['start']):>7}{flag(r['start_exact'])} "
              f"{clock(r['teaser']):>7}{flag(r['teaser_exact'])} "
              f"{clock(r['hint']):>7}    {' '.join(deltas)}")
    oe = (old or {}).get("outro_end"), (old or {}).get("outro_start")
    for label, val, ov in (("outro_start", m["outro_start"], oe[1]),
                           ("outro_end", m["outro_end"], oe[0])):
        d = f"{val - parse_mark(ov):+5.1f}" if ov is not None else "    –"
        print(f"    {label:<14} {clock(val):>7}                        {d}")
    print("\n? = estimated from sentence lengths (verify); unmarked = read off a silence gap.")


def write(m, ep_name, force):
    path = episode.path(episode.TIMELINE, ep_name)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    have = [r for r in data.get("windows") or []
            if any(r.get(k) is not None for k in ("start", "teaser", "hint"))]
    if (have or data.get("outro_start") is not None) and not force:
        raise SystemExit("timeline.json already has measured marks — add --force to "
                         "overwrite them.")
    data["windows"] = [{"word": r["word"], "start": round(r["start"], 1),
                        "teaser": round(r["teaser"], 1), "hint": round(r["hint"], 1)}
                       for r in m["rows"]]
    data["outro_start"] = round(m["outro_start"], 1)
    data["outro_end"] = round(m["outro_end"], 1)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(_compact_windows(json.dumps(data, ensure_ascii=False, indent=2)) + "\n",
                   encoding="utf-8")
    tmp.replace(path)
    print(f"Wrote {path}")


def main():
    ap = argparse.ArgumentParser(description="Measure timeline.json from the rendered video.")
    ap.add_argument("video")
    episode.add_argument(ap)
    ap.add_argument("--write", action="store_true", help="save the marks to timeline.json")
    ap.add_argument("--force", action="store_true", help="with --write: overwrite existing marks")
    ap.add_argument("--noise", type=float, default=NOISE_DB, metavar="DB",
                    help=f"silence threshold in dB (default {NOISE_DB})")
    ap.add_argument("--min-silence", type=float, default=MIN_SILENCE, metavar="S",
                    help=f"shortest gap counted, seconds (default {MIN_SILENCE})")
    args = ap.parse_args()

    name = episode.resolve_name(args.episode)
    print(f"Episode: {name}\nVideo:   {args.video}\n")
    script = load_script(name)
    m = measure(args.video, script, args.noise, args.min_silence)

    tl_path = episode.path(episode.TIMELINE, name)
    old = json.loads(tl_path.read_text(encoding="utf-8")) if tl_path.exists() else None
    report(m, old)
    for n in m["notes"]:
        print("WARN:", n)
    problems = check_order(m)
    for p in problems:
        print("PROBLEM:", p)
    if args.write:
        if problems:
            raise SystemExit("Not writing: the marks are out of order (see above).")
        write(m, name, args.force)
    elif not problems:
        print("Re-run with --write to save these.")


if __name__ == "__main__":
    main()
