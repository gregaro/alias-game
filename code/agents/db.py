"""SQLite-backed shared state. Agents write here; later agents read here."""
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "alias_game.db"


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_state (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                agent      TEXT NOT NULL,
                key        TEXT NOT NULL,
                value      TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def save_state(agent: str, key: str, value) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO agent_state (agent, key, value) VALUES (?, ?, ?)",
            (agent, key, json.dumps(value, ensure_ascii=False)),
        )


def all_states(agent: str, key: str, limit: int = 10) -> list:
    """Newest-first saved values. ORDER BY id: created_at only has
    second resolution, so same-second runs would sort ambiguously."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT value FROM agent_state WHERE agent=? AND key=? "
            "ORDER BY id DESC LIMIT ?",
            (agent, key, limit),
        ).fetchall()
    return [json.loads(row["value"]) for row in rows]


def latest_state(agent: str, key: str):
    # ORDER BY id, not created_at: timestamps have second resolution, so
    # two same-second saves would make "latest" ambiguous.
    with _conn() as conn:
        row = conn.execute(
            "SELECT value FROM agent_state WHERE agent=? AND key=? "
            "ORDER BY id DESC LIMIT 1",
            (agent, key),
        ).fetchone()
    return json.loads(row["value"]) if row else None


if __name__ == "__main__":
    # Standalone setup: create/upgrade the schema on a fresh machine.
    # Everything in init_db is idempotent (IF NOT EXISTS), so rerunning
    # after a schema change is always safe — future migrations go there.
    init_db()
    print(f"DB ready at {DB_PATH}")
