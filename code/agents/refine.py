"""The generic refinement loop: produce -> evaluate -> revise, up to N rounds.

This is what makes an "agent" an agent rather than a script with an LLM call
in it. A single model.invoke() has no way to notice that it wrote something
weak. This loop gives it one: a second model criticises the output, a third
tries to use it, and the writer gets another pass with concrete instructions.

The engine here knows nothing about hints, words, or Armenian. It knows:

  - run at most `max_rounds` times
  - keep the best candidate seen, never the last one
  - stop early when the spec accepts, or when nothing improves twice running
  - if nothing ever passed the hard gates, SAY SO rather than returning
    something that looks fine

The role-specific half — what to produce, what the gates are, when to
accept — lives in a spec object (see hint_loop.py). A second role (word
generation) adds a second spec and touches nothing in this file.

Ranking defaults to (gates_clean, min_score, total_score), but a spec may
override it — and hint_loop.py does, because a rubric score alone will
happily rank a candidate that gives the answer away above one with a
correct difficulty ladder. Improvement must be STRICT, so on an exact tie
the earlier round wins: equal quality with less churn is better, and a
later round that merely reshuffles is not an improvement.
"""
from dataclasses import dataclass, field


@dataclass
class Candidate:
    """One round's output plus everything we learned about it."""
    round: int
    payload: dict                          # role-specific (e.g. {"hints": [...]})
    gates: dict = field(default_factory=dict)   # name -> "pass" | "fail: why"
    scores: dict = field(default_factory=dict)  # name -> 1..5
    extra: dict = field(default_factory=dict)   # role-specific signals

    @property
    def gates_clean(self) -> bool:
        return all(v == "pass" for v in self.gates.values()) and bool(self.gates)

    @property
    def failed_gates(self) -> dict:
        return {k: v for k, v in self.gates.items() if v != "pass"}

    @property
    def min_score(self) -> int:
        # No scores at all means evaluation short-circuited on a hard gate.
        # That must rank below any scored candidate, not above it.
        return min(self.scores.values()) if self.scores else 0

    @property
    def total_score(self) -> int:
        return sum(self.scores.values())

    @property
    def rank(self) -> tuple:
        return (self.gates_clean, self.min_score, self.total_score)


@dataclass
class Result:
    """What the loop hands back for one target."""
    target: str
    best: Candidate
    status: str                # accepted | best_effort | gates_failed
    history: list              # every Candidate, in order
    rounds_used: int

    @property
    def needs_review(self) -> bool:
        return self.status != "accepted"


def refine(spec, target: str, trace, max_rounds: int = 3,
           plateau_rounds: int = 2) -> Result:
    """Run the loop for one target. Raises only if round 1 fails outright."""
    history: list[Candidate] = []
    best: Candidate | None = None
    stalls = 0
    # A spec may rank on signals the engine knows nothing about.
    rank_of = getattr(spec, "rank", None) or (lambda c: c.rank)

    trace.event("target_start", role=spec.name, target=target,
                max_rounds=max_rounds)

    for rnd in range(1, max_rounds + 1):
        previous = history[-1] if history else None

        # Some specs can tell that another round has no move left to make.
        # Paying for a provably identical candidate is worse than stopping.
        if previous is not None and getattr(spec, "exhausted", None) \
                and spec.exhausted(previous):
            trace.event("exhausted", role=spec.name, target=target, round=rnd)
            break

        try:
            payload = spec.produce(target, previous, trace, rnd)
            gates, scores, extra = spec.evaluate(target, payload, trace, rnd)
        except Exception as exc:
            # Round 1 blowing up means we have nothing at all — that is the
            # caller's `error` case. A later round failing is survivable:
            # we already hold a candidate, so stop and return it.
            trace.event("round_error", role=spec.name, target=target,
                        round=rnd, error=f"{type(exc).__name__}: {exc}")
            if not history:
                raise
            break

        cand = Candidate(round=rnd, payload=payload, gates=gates,
                         scores=scores, extra=extra)
        history.append(cand)

        accepted = spec.accepts(cand)
        trace.event("round", role=spec.name, target=target, round=rnd,
                    gates=cand.gates, scores=cand.scores, extra=cand.extra,
                    payload=cand.payload, accepted=accepted)

        if best is None or rank_of(cand) > rank_of(best):  # strict: ties keep earlier
            best, stalls = cand, 0
        else:
            stalls += 1

        if accepted:
            trace.event("target_end", role=spec.name, target=target,
                        status="accepted", best_round=rnd, rounds_used=rnd)
            return Result(target, cand, "accepted", history, rnd)

        if stalls >= plateau_rounds:
            # Two rounds running with no improvement means the writer is
            # thrashing, not converging. Paying for a third won't help.
            trace.event("plateau", role=spec.name, target=target, round=rnd)
            break

    status = "best_effort" if best.gates_clean else "gates_failed"
    trace.event("target_end", role=spec.name, target=target, status=status,
                best_round=best.round, rounds_used=len(history))
    return Result(target, best, status, history, len(history))


if __name__ == "__main__":
    # Self-test the engine with a scripted spec — no models, no network, no
    # cost. Run `python refine.py`. Same idea as normalize.py's self-test.
    from tracing import NullTrace

    PASS, FAIL = {"g": "pass"}, {"g": "fail: nope"}

    class _FakeSpec:
        """Replays a script of (gates, scores) per round."""
        name = "test"

        def __init__(self, script, boom_at=None):
            self.script = script
            self.boom_at = boom_at

        def produce(self, target, previous, trace, rnd):
            if rnd == self.boom_at:
                raise RuntimeError("scripted failure")
            return {"round": rnd}

        def evaluate(self, target, payload, trace, rnd):
            gates, scores = self.script[min(rnd, len(self.script)) - 1]
            return gates, scores, {}

        def accepts(self, cand):
            return cand.gates_clean and cand.min_score >= 4

    def check(name, got, want):
        ok = got == want
        print(f"[{'ok  ' if ok else 'FAIL'}] {name}: {got}"
              + ("" if ok else f"  (want {want})"))
        return ok

    S = lambda n: {"a": n, "b": n}          # noqa: E731  scores helper
    results = []

    r = refine(_FakeSpec([(PASS, S(5))]), "t", NullTrace())
    results.append(check("accepts on round 1",
                         (r.status, r.best.round, r.rounds_used),
                         ("accepted", 1, 1)))

    # Round 2 is strictly better and still not acceptable -> keep round 2.
    r = refine(_FakeSpec([(PASS, S(1)), (PASS, S(3)), (PASS, S(3))]), "t",
               NullTrace())
    results.append(check("keeps the better later round",
                         (r.status, r.best.round), ("best_effort", 2)))

    # Identical rounds: ties must keep the EARLIER one (less churn).
    r = refine(_FakeSpec([(PASS, S(3))]), "t", NullTrace())
    results.append(check("ties keep the earlier round",
                         (r.status, r.best.round, r.rounds_used),
                         ("best_effort", 1, 3)))

    # Plateau: with headroom to spare, two flat rounds still stop the loop.
    r = refine(_FakeSpec([(PASS, S(3))]), "t", NullTrace(), max_rounds=5)
    results.append(check("plateau stops before max_rounds",
                         r.rounds_used, 3))

    # Never clears the gates -> says so, rather than returning a clean-looking
    # candidate.
    r = refine(_FakeSpec([(FAIL, S(5))]), "t", NullTrace())
    results.append(check("gates never pass -> gates_failed",
                         (r.status, r.needs_review), ("gates_failed", True)))

    # Round 1 blowing up has nothing to return: it must raise.
    try:
        refine(_FakeSpec([(PASS, S(5))], boom_at=1), "t", NullTrace())
        results.append(check("round 1 error raises", False, True))
    except RuntimeError:
        results.append(check("round 1 error raises", True, True))

    # A later round failing is survivable — we already hold a candidate.
    r = refine(_FakeSpec([(PASS, S(3))], boom_at=2), "t", NullTrace())
    results.append(check("later error keeps what we had",
                         (r.status, r.best.round, r.rounds_used),
                         ("best_effort", 1, 1)))

    print(f"\n{sum(results)}/{len(results)} passed")
    raise SystemExit(0 if all(results) else 1)
