"""Append-only JSONL trace of one agent run.

Deliberately the same shape as the scorer's show logs
(code/logs/<episode>/<timestamp>.jsonl) and for the same reason: the
failures are the ones you can't see. A one-shot script fails visibly — its
bad output is right there on the terminal. A refinement loop fails
invisibly: round 2 quietly scored worse than round 1, the critic's note
contradicted the kept-hints block, the solver never had a chance. None of
that survives unless every step leaves a line behind.

One file per RUN (not per day): a throwaway experiment and the run that
actually fed a show are different facts, and neither should overwrite the
other. review_agents.py reads these back.

Two levels of detail live here on purpose:

  - llm_call  — the generic layer: model, latency, tokens, and the RENDERED
                prompt. Rendered, not the input variables: when a revise
                round goes sideways it is almost always because the critic
                notes and the kept-hints block assembled into something
                self-contradictory, and you cannot see that from the inputs.
  - round     — the decision layer: gates, scores, solver result, verdict.
                No generic tracing tool knows to ask for this, which is why
                it is written by hand.

Gitignored (code/logs/ already is). These files carry no viewer data, but
they do carry your entire show before it airs.
"""
import json
import threading
import time
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / "logs" / "agents"


class Trace:
    """Append-only JSONL writer. Thread-safe: words refine in parallel."""

    def __init__(self, kind: str = "run"):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.path = LOG_DIR / f"{kind}-{stamp}.jsonl"
        self._lock = threading.Lock()
        self._file = self.path.open("a", encoding="utf-8")
        self.event("run_start", kind=kind)

    def event(self, event: str, **fields) -> None:
        record = {"ts": round(time.time(), 3), "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._file.write(line + "\n")
            # Flush every line: a run that dies mid-loop is exactly the run
            # whose trace you need, so nothing may sit in a buffer.
            self._file.flush()

    def close(self) -> None:
        self.event("run_end")
        with self._lock:
            self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class NullTrace:
    """No-op stand-in, so callers never need `if trace:` branches."""

    path = None

    def event(self, event: str, **fields) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
