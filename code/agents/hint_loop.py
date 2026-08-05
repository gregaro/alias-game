"""The hint-generation half of the refinement loop: gates, prompts, accept.

refine.py runs the loop; this file knows what "good hints" means. Three
models, three vendors, so nothing grades its own work:

    hint_generator  (Gemini)     writes and revises
    hint_critic     (Claude)     scores the rubric, says what to fix
    hint_solver     (gpt-4o-mini) guesses BLIND — never sees the word

Order per round: code gates -> solver -> critic. Two reasons that order and
not the obvious one:

  1. A hint that mechanically contains its own target word does not need a
     critic's opinion, it needs a rewrite. Gating in code first skips two
     paid calls on exactly the rounds that need the budget least.
  2. The solver runs BEFORE the critic so the critic can be shown what a
     blind guesser actually did. A critic that knows the answer is a bad
     judge of guessability — it can't un-know the word. Measuring beats
     judging, so the measurement goes in first.

## Only hints 1 and 3 are measured

The show speaks the teaser and the long hint; hint 2 is a written spare that
never reaches the audience (see CLAUDE.md). So the solver is given rung 1 =
hint 1 alone, then rung 2 = hints 1 + 3. Measuring a ladder the audience
never climbs would be measuring the wrong thing.

`solved_at` is therefore 1 (solved on the teaser — too easy), 3 (solved on
the long hint — correct), or None (never solved — likely too obscure).
"""
import sys
import unicodedata
from pathlib import Path

# Reuse the scorer's Armenian matcher for the solver's guesses, so the bar a
# guess has to clear is exactly the bar a viewer's chat message clears —
# same normalization, same one-character typo tolerance. Nothing heavy: it
# is stdlib-only.
sys.path.insert(0, str(Path(__file__).parent.parent / "scorer"))
from normalize import matches  # noqa: E402

GATE_NAMES = ("leak", "translation", "armenian", "teaser_length",
              "no_punching", "facts")


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold()


def leak_check(word: str, hints: list[str]) -> list[str]:
    """A hint must never contain the target word (or any word of a phrase).

    Deterministic, free, and catches the one failure that is fatal on air —
    so it runs in code before any model is asked for an opinion.
    """
    return [
        f"hint {i} contains '{part}'"
        for i, hint in enumerate(hints, 1)
        for part in _norm(word).split()
        if part in _norm(hint)
    ]


class HintSpec:
    """The role-specific half of the loop for hint generation."""

    name = "hints"

    def __init__(self, orch, settings: dict, variants: dict | None = None):
        self.orch = orch
        self.accept_min_score = settings.get("accept_min_score", 4)
        # word -> hand-curated accepted answers from past episodes. Without
        # these the solver is judged more harshly than a viewer: on a real
        # run (2026-08-04) it answered «ծույլ» for «ալարկոտ» — a synonym
        # CLAUDE.md itself lists as an accepted answer — and every round was
        # scored as "too obscure" for a hint ladder that was working.
        self.variants = variants or {}

    # ---------------------------------------------------------------- produce

    def produce(self, word: str, previous, trace, rnd: int) -> dict:
        if previous is None:
            result = self.orch.run("hint_generator", f"WORD: {word}",
                                   persist=False, trace=trace,
                                   target=word, round=rnd, role="generator")
        else:
            result = self.orch.run(
                "hint_generator",
                f"WORD: {word}\n\n"
                "Your previous attempt was reviewed. Fix ONLY what the review "
                "flags. Any hint listed in keep_unchanged must come back "
                "word-for-word identical — rewriting a hint that already works "
                "is a regression, not an improvement. Return all 3 hints in "
                "the usual JSON shape.",
                context=self._review_context(previous),
                persist=False, trace=trace,
                target=word, round=rnd, role="generator",
            )

        hints = result.get("hints")
        if not isinstance(hints, list) or len(hints) != 3:
            raise ValueError(f"generator returned {len(hints or [])} hints, need 3")
        hints = [str(h).strip() for h in hints]

        # Enforce keep_unchanged in CODE rather than trusting the prompt.
        # Observed on a real run (2026-08-04, «հարսանիք»): the critic said
        # keep hints 1 and 3, and the writer rewrote hint 1 regardless. That
        # is precisely the collateral damage `keep` exists to stop, and a
        # polite instruction is not a guarantee. Restoring is free.
        if previous is not None:
            was = previous.payload["hints"]
            restored = []
            for n in self._effective_keep(previous):
                i = n - 1
                if hints[i] != was[i]:
                    hints[i] = was[i]
                    restored.append(n)
            if restored:
                trace.event("keep_enforced", target=word, round=rnd,
                            hints=restored)

        return {"hints": hints}

    @staticmethod
    def _effective_keep(previous) -> list[int]:
        """Hints the writer is genuinely forbidden to touch.

        Two things override the critic's `keep`, both seen on real runs
        (2026-08-04):

        1. A critic that both keeps a hint AND writes a note about it is
           contradicting itself. The note is the more specific signal, so a
           noted hint is never kept — otherwise keep=[1,2,3] plus a note on
           hint 1 made the code restore every hint and reproduce an
           identical candidate, a full round of calls to change nothing.

        2. The solver's verdict beats the critic's taste. If the teaser gave
           the answer away, hint 1 has to move whether the critic likes it or
           not; if nothing reached the word, hint 3 has to move. The critic
           knows the answer and cannot feel either problem — that is the
           whole reason a blind solver is in the loop.
        """
        noted = {int(n) for n in (previous.extra.get("notes") or {})
                 if str(n).isdigit()}
        keep = {int(n) for n in previous.extra.get("keep", [])
                if str(n).isdigit() and 1 <= int(n) <= 3}
        solved_at = previous.extra.get("solved_at")
        if solved_at == 1:
            noted.add(1)          # teaser gives it away -> it must change
        elif solved_at is None:
            noted.add(3)          # nothing landed -> the long hint must change
        return sorted(keep - noted)

    @classmethod
    def exhausted(cls, previous) -> bool:
        """True when another round provably cannot change anything.

        If the gates are clean and the critic wants all three hints kept with
        nothing noted, the writer has no move left. Stopping here is not
        giving up — it is declining to pay for an identical candidate.
        """
        return (previous.gates_clean
                and len(cls._effective_keep(previous)) == 3)

    @staticmethod
    def _review_context(previous) -> dict:
        """What the writer is told about its last attempt.

        The whole revise prompt is assembled here, and the rendered version
        is what lands in the trace — because when a revise round goes
        sideways it is nearly always these pieces contradicting each other.
        """
        hints = previous.payload["hints"]
        solved_at = previous.extra.get("solved_at")
        if solved_at == 1:
            solver_line = ("A blind solver named the word from the teaser "
                           "alone. The teaser gives too much away.")
        elif solved_at == 3:
            solver_line = "A blind solver reached the word on hint 3, as intended."
        else:
            solver_line = ("A blind solver never reached the word, even with "
                           "hints 1 and 3 together. Hint 3 is not landing.")

        return {
            "previous_hints": {str(i): h for i, h in enumerate(hints, 1)},
            "gate_failures": previous.failed_gates,
            "critic_notes": previous.extra.get("notes", {}),
            "keep_unchanged": HintSpec._effective_keep(previous),
            "blind_solver": solver_line,
            "blind_solver_guesses": previous.extra.get("guesses", {}),
            "note": ("Only hints 1 and 3 are spoken on air; hint 2 is a spare. "
                     "The ladder that matters runs 1 -> 3."),
        }

    # --------------------------------------------------------------- evaluate

    def evaluate(self, word: str, payload: dict, trace, rnd: int):
        hints = payload["hints"]

        # 1. Code gates first — free, and a leak makes the rest moot.
        leaks = leak_check(word, hints)
        gates = {name: "pass" for name in GATE_NAMES}
        if leaks:
            gates["leak"] = "fail: " + "; ".join(leaks)
            trace.event("gate_shortcircuit", target=word, round=rnd,
                        reason=gates["leak"])
            return gates, {}, {"solved_at": None, "notes": {}, "keep": []}

        # 2. Blind solver — measured, not judged.
        solved_at, guesses = self._solve(word, hints, trace, rnd)

        # 3. Critic, told what the solver did.
        review = self.orch.run(
            "hint_critic",
            f"Judge these hints for the target word: {word}",
            context={
                "word": word,
                "hints": {str(i): h for i, h in enumerate(hints, 1)},
                "blind_solver_solved_at": solved_at,
                "blind_solver_guesses": guesses,
            },
            persist=False, trace=trace,
            target=word, round=rnd, role="critic",
        )

        gates.update({k: v for k, v in (review.get("gates") or {}).items()
                      if k in GATE_NAMES})
        scores = {k: int(v) for k, v in (review.get("scores") or {}).items()}
        extra = {
            "solved_at": solved_at,
            "guesses": guesses,
            "notes": review.get("notes") or {},
            "keep": review.get("keep") or [],
            "verdict": review.get("verdict"),
        }
        return gates, scores, extra

    def _solve(self, word: str, hints: list[str], trace, rnd: int):
        """Blind-guess the word, one spoken rung at a time.

        Separate calls per rung on purpose: a model shown all the hints at
        once cannot honestly report what it would have guessed from the
        teaser alone — it has already read the rest. Stop at the first rung
        that lands, which is also the cheaper path.
        """
        rungs = [(1, [hints[0]]), (3, [hints[0], hints[2]])]
        guesses: dict[str, list] = {}
        # Judge the guess against everything a viewer could type and be
        # scored for, not just the literal target string.
        accepted = [word] + list(self.variants.get(word, []))
        for rung, shown in rungs:
            numbered = "\n".join(f"{i}. {h}" for i, h in enumerate(shown, 1))
            result = self.orch.run(
                "hint_solver",
                f"Guess the Armenian word from these hints:\n{numbered}",
                persist=False, trace=trace,
                target=word, round=rnd, role=f"solver_rung{rung}",
            )
            got = [str(g) for g in (result.get("guesses") or [])]
            guesses[f"after_hint_{rung}"] = got
            if any(matches(g, accepted) for g in got):
                trace.event("solver_hit", target=word, round=rnd, rung=rung,
                            guesses=got)
                return rung, guesses
        trace.event("solver_miss", target=word, round=rnd, guesses=guesses)
        return None, guesses

    # ------------------------------------------------------- rank and accepts

    # How good a solver outcome is, for RANKING the fallback candidate.
    # Observed on a real run (2026-08-04, «հարսանիք»): round 1 had the right
    # ladder (solved_at 3) and round 2 gave the answer away in the teaser
    # (solved_at 1) while scoring one rubric point higher — so a
    # score-only ranking kept the worse ladder. The ladder has to outrank
    # taste points.
    #
    # Too easy beats never-solved: a giveaway teaser still leaves a playable
    # round (speed decay handles it), whereas a word nobody can reach is dead
    # air and a leaderboard that doesn't move.
    _SOLVER_RANK = {3: 2, 1: 1, None: 0}

    def rank(self, cand) -> tuple:
        return (cand.gates_clean,
                self._SOLVER_RANK.get(cand.extra.get("solved_at"), 0),
                cand.min_score,
                cand.total_score)

    def accepts(self, cand) -> bool:
        """Humor may never be bought with clarity.

        Hence min() over the scores rather than a total — a 5 for humor
        cannot pay for a 2 in escalation — and hence the solver sitting
        INSIDE the accept condition rather than beside it.
        """
        return (cand.gates_clean
                and cand.scores
                and cand.min_score >= self.accept_min_score
                and cand.extra.get("solved_at") == 3)

    # ---------------------------------------------------------------- display

    @staticmethod
    def summary(result) -> str:
        cand = result.best
        if cand.scores:
            scores = " ".join(f"{k[:3]} {v}" for k, v in sorted(cand.scores.items()))
        else:
            scores = "(unscored)"
        solved = cand.extra.get("solved_at")
        solver = f"solver@{solved}" if solved else "solver missed"
        line = (f"{result.status:<12} round {cand.round}  {scores}  {solver}")
        if not cand.gates_clean:
            line += "  <- " + "; ".join(f"{k}: {v}" for k, v in cand.failed_gates.items())
        return line


if __name__ == "__main__":
    # Self-test with scripted models — no network, no cost. `python hint_loop.py`.
    # Two of these are regressions found on a real run (2026-08-04); they are
    # here so they cannot come back quietly.
    from refine import refine
    from tracing import NullTrace

    WORD = "Յուպիտեր"
    GOOD = ["մոլորակների հսկան", "գազային աշխարհ",
            "արեգակնային համակարգի ամենամեծը"]
    ALT = ["բոլորովին այլ ակնարկ", "երկրորդ տարբերակ", "երրորդ տարբերակ"]
    LEAKY = ["Յուպիտեր մոլորակ", "գազային", "մեծ մոլորակ"]

    def critic(score=5, keep=None, notes=None):
        return {"gates": {n: "pass" for n in GATE_NAMES},
                "scores": {"humor": score, "escalation": score,
                           "naturalness": score, "variety": score},
                "notes": notes or {}, "keep": keep or [], "verdict": "accept"}

    class _FakeOrch:
        """Scripted replies per round; records what was actually called."""

        def __init__(self, script):
            self.script = script
            self.calls = []

        def run(self, name, user_input, context=None, persist=True,
                trace=None, **tags):
            rnd = tags.get("round", 1)
            self.calls.append((name, rnd))
            step = self.script[min(rnd, len(self.script))]
            if name == "hint_generator":
                return {"hints": list(step["hints"])}
            if name == "hint_critic":
                return step["critic"]
            rung = 1 if tags.get("role") == "solver_rung1" else 3
            return {"guesses": step["solver"].get(rung, ["ոչինչ"])}

    def go(script, variants=None, **kw):
        orch = _FakeOrch(script)
        spec = HintSpec(orch, {"accept_min_score": 4}, variants)
        return orch, refine(spec, WORD, NullTrace(), **kw)

    def check(name, got, want):
        ok = got == want
        print(f"[{'ok  ' if ok else 'FAIL'}] {name}: {got}"
              + ("" if ok else f"  (want {want})"))
        return ok

    results = []

    # Clean hints, solver lands on hint 3 -> accepted, 4 calls.
    orch, r = go({1: {"hints": GOOD, "critic": critic(5),
                      "solver": {1: ["մարս"], 3: [WORD]}}})
    results.append(check("accepts a clean first round",
                         (r.status, r.best.round), ("accepted", 1)))
    results.append(check("costs 1 gen + 2 solver + 1 critic",
                         len(orch.calls), 4))

    # A leak must short-circuit BEFORE any paid opinion is bought.
    orch, r = go({1: {"hints": LEAKY, "critic": critic(5), "solver": {}},
                  2: {"hints": GOOD, "critic": critic(5),
                      "solver": {1: ["մարս"], 3: [WORD]}}})
    results.append(check("leak skips solver and critic",
                         [c for c in orch.calls if c[1] == 1],
                         [("hint_generator", 1)]))
    results.append(check("leak recovers next round",
                         (r.status, r.best.round), ("accepted", 2)))

    # Solved off the teaser is never acceptable, however good the prose.
    _, r = go({1: {"hints": GOOD, "critic": critic(5),
                   "solver": {1: [WORD], 3: [WORD]}}})
    results.append(check("giveaway teaser never accepts",
                         (r.status, r.best.extra["solved_at"]),
                         ("best_effort", 1)))

    # REGRESSION (real run, «հարսանիք»): round 2 scored higher but gave the
    # answer away in the teaser. The correct LADDER must outrank taste points.
    _, r = go({1: {"hints": GOOD, "critic": critic(4),
                   "solver": {1: ["մարս"], 3: [WORD]}},
               2: {"hints": ALT, "critic": critic(5),
                   "solver": {1: [WORD], 3: [WORD]}}})
    results.append(check("right ladder outranks a higher score",
                         (r.best.round, r.best.extra["solved_at"]), (1, 3)))

    # REGRESSION (same run): the writer rewrote a hint the critic said to
    # keep. Enforced in code now, so the kept hint comes back verbatim.
    _, r = go({1: {"hints": GOOD, "critic": critic(3, keep=[2]),
                   "solver": {1: ["մարս"], 3: ["մարս"]}},
               2: {"hints": ALT, "critic": critic(3),
                   "solver": {1: ["մարս"], 3: ["մարս"]}}})
    results.append(check("kept hint survives a rewrite",
                         r.history[1].payload["hints"][1], GOOD[1]))
    results.append(check("unkept hints still change",
                         r.history[1].payload["hints"][0], ALT[0]))

    # REGRESSION (same run): a critic that both keeps a hint and writes a note
    # about it is contradicting itself; the note wins, or the writer is left
    # with no legal move.
    _, r = go({1: {"hints": GOOD, "critic": critic(3, keep=[1, 2],
                                                  notes={"1": "sharpen it"}),
                   "solver": {1: ["մարս"], 3: ["մարս"]}},
               2: {"hints": ALT, "critic": critic(3),
                   "solver": {1: ["մարս"], 3: ["մարս"]}}})
    results.append(check("a noted hint is not kept",
                         r.history[1].payload["hints"][0], ALT[0]))
    results.append(check("a quietly kept hint still holds",
                         r.history[1].payload["hints"][1], GOOD[1]))

    # REGRESSION (same run): critic keeps ALL THREE with nothing noted while
    # scoring below the bar. No move exists, so don't buy another round.
    orch, r = go({1: {"hints": GOOD, "critic": critic(3, keep=[1, 2, 3]),
                      "solver": {1: ["մարս"], 3: [WORD]}}})
    results.append(check("no legal move -> stop instead of paying",
                         (r.status, r.rounds_used), ("best_effort", 1)))
    results.append(check("and it really stopped calling", len(orch.calls), 4))

    # REGRESSION (same run): the critic loved hints the solver cracked off
    # the teaser. A measured failure outranks the critic's keep, so the loop
    # must NOT call it exhausted — hint 1 has to move.
    _, r = go({1: {"hints": GOOD, "critic": critic(5, keep=[1, 2, 3]),
                   "solver": {1: [WORD], 3: [WORD]}},
               2: {"hints": ALT, "critic": critic(5),
                   "solver": {1: ["մարս"], 3: [WORD]}}})
    results.append(check("giveaway teaser forces another round",
                         (r.status, r.best.round), ("accepted", 2)))
    results.append(check("and the teaser is what changed",
                         (r.history[1].payload["hints"][0],
                          r.history[1].payload["hints"][1]), (ALT[0], GOOD[1])))

    # Mirror case: nothing reached the word, so hint 3 must move even if kept.
    _, r = go({1: {"hints": GOOD, "critic": critic(5, keep=[1, 2, 3]),
                   "solver": {1: ["մարս"], 3: ["մարս"]}},
               2: {"hints": ALT, "critic": critic(5),
                   "solver": {1: ["մարս"], 3: [WORD]}}})
    results.append(check("unsolved forces hint 3 to move",
                         (r.history[1].payload["hints"][2],
                          r.history[1].payload["hints"][0]), (ALT[2], GOOD[0])))

    # The solver is held to the viewer's bar: a one-character typo counts.
    _, r = go({1: {"hints": GOOD, "critic": critic(5),
                   "solver": {1: ["մարս"], 3: ["Յուպիտեռ"]}}})
    results.append(check("solver typo matches like a viewer's would",
                         r.status, "accepted"))

    # REGRESSION (real run, «ալարկոտ»): the solver answered a curated synonym
    # and was scored as having failed. A guess a viewer would be paid for
    # must count here too.
    _, r = go({1: {"hints": GOOD, "critic": critic(5),
                   "solver": {1: ["մարս"], 3: ["գազային հսկա"]}}},
              variants={WORD: ["գազային հսկա", "Jupiter"]})
    results.append(check("a curated answer variant counts as solved",
                         (r.status, r.best.extra["solved_at"]),
                         ("accepted", 3)))

    print(f"\n{sum(results)}/{len(results)} passed")
    raise SystemExit(0 if all(results) else 1)
