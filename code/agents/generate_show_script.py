"""Stage four of the show pipeline: hints -> the clip script the host reads.

The show_scripter agent writes the frame (intro, per-word lead-ins,
reveals, outro) in the same party-friend voice as the hints; this script
weaves the existing hints into it and writes two artifacts next to
questions.json:

  show_script.json  structured segments, for future TTS/clip automation
  show_script.txt   human reference: window boundaries + labels, for
                    understanding flow and scorer timing (NOT for TTS —
                    its markers would be read aloud)
  show_script_tts.txt  paste-ready ElevenLabs sheet: one clean Armenian
                    block per window, the only tag being the <break> beat

Each word is a 2-hint round inside ONE window_seconds window: the teaser
opens it, a 3-4s beat later the confident closer lands, then guessing
time until the window closes. The concrete middle hint is kept as
spare_hint (never read on air).

Windows are fixed-length and contiguous so the parallel scorer stays
locked to the video: chat_scorer opens window 1 on one Enter and
auto-advances every window_seconds with NO gaps between words. To match
that, each window is one self-contained block that OPENS by announcing
the PREVIOUS word's answer (safe — that window is already closed), then
lead-in, teaser, beat, hint, guessing. The last word's answer lands in
the outro (after the final window). Nothing is spoken between windows.

NOTE: with the reveal + lead-in now inside the window, window_seconds
must be large enough to fit reveal_prev + lead-in + both hints + real
guessing time — likely more than 20s. Set it from the recorded clip
length once TTS audio exists.
"""
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()  # load API keys before building any model

import db
from orchestrator import Orchestrator

HERE = os.path.dirname(os.path.abspath(__file__))
QUESTIONS_FILE = os.path.join(HERE, "../questions/questions.json")
OUT_JSON = os.path.join(HERE, "../questions/show_script.json")
OUT_TXT = os.path.join(HERE, "../questions/show_script.txt")
OUT_TTS = os.path.join(HERE, "../questions/show_script_tts.txt")

# The game beat between teaser and hint. SSML break, Eleven v2 models only
# (v3 rejects SSML — swap for [pause]). 3s is ElevenLabs' per-break maximum.
BEAT = '<break time="3.0s" />'


def _write_atomic(path: str, text: str):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def episode_words(frame: dict, hints_by_word: dict) -> list[dict]:
    """Two hints per round: the teaser opens the window, a 3-4s beat later
    the confident closer lands, and the whole word runs inside one window.
    The concrete middle hint stays as spare material (never read on air)."""
    words = []
    for w in frame["words"]:
        hints = hints_by_word[w["word"]]
        words.append({
            "word": w["word"],
            "lead_in": w["lead_in"],
            "teaser": hints[0],
            "closer": hints[-1],
            "spare_hint": hints[1] if len(hints) > 2 else None,
            "reveal": w["reveal"],
        })
    return words


def assemble_tts(intro: str, windows: list[dict], final_reveal: str,
                 outro: str) -> str:
    """Paste-ready ElevenLabs sheet: one clean Armenian block per window,
    the ONLY tag being the teaser->hint beat. Everything a human needs to
    know is a `#` comment line — never selected, never narrated. Copy one
    block at a time into the TTS box; the guessing time after the hint is
    added later in video editing, not spoken."""
    lines = [
        "# ElevenLabs paste sheet. Copy ONE Armenian block at a time into the",
        f"# TTS box. Only tag is the beat: {BEAT} (Eleven v2 models). On v3,",
        "# replace it with [pause]. NEVER paste the # comment lines.",
        "",
        "# --- INTRO ---",
        intro,
        "",
    ]
    for i, w in enumerate(windows, 1):
        parts = [w["reveal_prev"]] if w["reveal_prev"] else []
        parts += [w["lead_in"], w["teaser"]]
        speech = " ".join(parts) + " " + BEAT + " " + w["closer"]
        lines += [f"# --- WINDOW {i}: {w['word']} ---", speech, ""]
    lines += ["# --- OUTRO ---", final_reveal + " " + outro, ""]
    return "\n".join(lines)


def assemble_txt(intro: str, words: list[dict], outro: str,
                 window_seconds: int) -> str:
    n = len(words)
    lines = [
        "=== INTRO (plays BEFORE the clock — record separately) ===",
        intro,
        "",
        f"Windows below are each exactly {window_seconds}s and run "
        "back-to-back with NO gaps. Start the scorer as WINDOW 1 opens; it "
        f"auto-advances every {window_seconds}s. Each window reveals the "
        "PREVIOUS word's answer at its start (safe — that window is already "
        "closed), then hints the current word.",
        "",
    ]
    for i, w in enumerate(words, 1):
        lines.append(f"===== WINDOW {i}/{n} — {window_seconds}s — "
                     f"SCORING: {w['word']} =====")
        lines.append(f">>> OPEN 0:00  (scorer begins counting «{w['word']}»)")
        if i > 1:
            lines.append(f"[reveal «{words[i - 2]['word']}»] "
                         f"{words[i - 2]['reveal']}")
        lines += [
            f"[lead-in] {w['lead_in']}",
            f"[teaser]  {w['teaser']}",
            "[pause ~3-4s]",
            f"[hint]    {w['closer']}",
            "[viewers keep typing until the window ends]",
            f"<<< CLOSE 0:{window_seconds:02d}  (next window opens immediately)",
            "",
        ]
    # The last word has no next window to carry its reveal, so it opens the outro.
    lines += [
        "=== OUTRO (after the last window closes) ===",
        f"[reveal «{words[-1]['word']}»] {words[-1]['reveal']}",
        outro,
        "",
    ]
    return "\n".join(lines)


def main():
    show = db.latest_state("hint_generator", "last_hints")
    if not show:
        raise SystemExit("No hints in the DB — run generate_hints.py first.")
    hints_by_word = {e["word"]: e["hints"] for e in show}

    window_seconds = 25
    if os.path.exists(QUESTIONS_FILE):
        with open(QUESTIONS_FILE, encoding="utf-8") as f:
            window_seconds = json.load(f).get("window_seconds", 25)

    orch = Orchestrator()
    frame = orch.run(
        "show_scripter",
        "Write the host frame for this episode.",
        context={"words": show, "window_seconds": window_seconds},
        persist=False,
    )

    # The frame must cover exactly our words, in order — a mismatch means
    # hallucinated or dropped words, and the clip script would desync.
    got = [w["word"] for w in frame.get("words", [])]
    want = [e["word"] for e in show]
    if got != want:
        raise SystemExit(f"Frame word list mismatch:\n  want {want}\n  got  {got}")

    # lead_ins must not leak any target word of the episode.
    for w in frame["words"]:
        for target in want:
            if target.casefold() in w["lead_in"].casefold():
                print(f"warning: lead_in for {w['word']!r} mentions {target!r} "
                      "— review before recording", file=sys.stderr)

    db.save_state("show_scripter", "last_script", frame)
    words = episode_words(frame, hints_by_word)
    # Each window is one scored unit: it announces the PREVIOUS word's answer
    # at its start (reveal_prev, null for window 1), then hints its own word.
    windows = [
        {
            "word": w["word"],
            "reveal_prev": words[i - 1]["reveal"] if i > 0 else None,
            "lead_in": w["lead_in"],
            "teaser": w["teaser"],
            "closer": w["closer"],
            "spare_hint": w["spare_hint"],
        }
        for i, w in enumerate(words)
    ]
    script = {
        "window_seconds": window_seconds,
        "intro": frame["intro"],
        "windows": windows,
        "final_reveal": words[-1]["reveal"],  # spoken in the outro
        "outro": frame["outro"],
    }
    _write_atomic(OUT_JSON, json.dumps(script, ensure_ascii=False, indent=2) + "\n")
    txt = assemble_txt(frame["intro"], words, frame["outro"], window_seconds)
    _write_atomic(OUT_TXT, txt)
    tts = assemble_tts(frame["intro"], windows, words[-1]["reveal"], frame["outro"])
    _write_atomic(OUT_TTS, tts)

    print(f"Wrote:\n  {OUT_TXT}  (human reference)\n"
          f"  {OUT_JSON}  (automation)\n"
          f"  {OUT_TTS}  (paste into ElevenLabs)\n")
    print(tts)


if __name__ == "__main__":
    main()
