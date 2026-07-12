"""Where each show's files live.

One folder per episode, so an old show stays runnable instead of being
overwritten by the next one:

    code/questions/
    ├── current_episode          one line, e.g. "ep2-2026-07-12"
    └── episodes/
        ├── ep1-2026-07-11/      questions.json, timeline.json, show_script.*
        └── ep2-2026-07-12/      same

The pointer file means show night needs no extra typing — `chat_scorer.py`
just runs the current episode. Pass `--episode ep1-2026-07-11` to replay an
old one.

Shared by both code/agents/ and code/scorer/, which sit one level down and add
this directory to sys.path to import it.
"""
import re
from datetime import date as _date
from pathlib import Path

HERE = Path(__file__).parent                    # code/
QUESTIONS_DIR = HERE / "questions"
EPISODES_DIR = QUESTIONS_DIR / "episodes"
CURRENT_FILE = QUESTIONS_DIR / "current_episode"

# The five files that make up an episode.
QUESTIONS = "questions.json"
TIMELINE = "timeline.json"
SCRIPT_TXT = "show_script.txt"
SCRIPT_JSON = "show_script.json"
SCRIPT_TTS = "show_script_tts.txt"


def _num(name: str) -> int:
    """Leading episode number, for sorting. ep10 must sort after ep9, so sort on
    the number rather than the string."""
    m = re.match(r"ep(\d+)", name)
    return int(m.group(1)) if m else 0


def list_episodes() -> list[str]:
    """Episode names, oldest first."""
    if not EPISODES_DIR.is_dir():
        return []
    return sorted((p.name for p in EPISODES_DIR.iterdir() if p.is_dir()),
                  key=lambda n: (_num(n), n))


def current() -> str:
    """The episode the pointer file names."""
    if not CURRENT_FILE.is_file():
        raise SystemExit(
            f"No current episode: {CURRENT_FILE} is missing.\n"
            "Run: python code/agents/new_episode.py")
    name = CURRENT_FILE.read_text(encoding="utf-8").strip()
    if not name:
        raise SystemExit(f"{CURRENT_FILE} is empty — write an episode name into it.")
    return name


def set_current(name: str) -> None:
    CURRENT_FILE.write_text(name + "\n", encoding="utf-8")


def resolve(name: str | None = None) -> Path:
    """Directory of `name`, or of the current episode.

    Fails loudly rather than falling back to anything: silently running the
    WRONG episode on show night is the worst outcome this module can produce."""
    name = name or current()
    d = EPISODES_DIR / name
    if not d.is_dir():
        have = ", ".join(list_episodes()) or "(none)"
        raise SystemExit(f"Episode {name!r} not found under {EPISODES_DIR}.\n"
                         f"Available: {have}")
    return d


def path(filename: str, name: str | None = None) -> Path:
    """Full path to one file inside an episode."""
    return resolve(name) / filename


def next_name(on: str | None = None) -> str:
    """Name for the next episode: ep<N+1>-YYYY-MM-DD."""
    n = max((_num(e) for e in list_episodes()), default=0)
    return f"ep{n + 1}-{on or _date.today().isoformat()}"


def add_argument(parser) -> None:
    """Standard --episode flag, so every entry point spells it the same way."""
    parser.add_argument(
        "--episode", metavar="NAME", default=None,
        help="episode to use (default: whatever current_episode names)")
