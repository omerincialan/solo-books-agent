"""A small SQLite log of the conversation, so the agent remembers the last few turns."""
import datetime
import sqlite3
from . import config


def _connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        number TEXT, role TEXT, content TEXT, created_at TEXT)""")
    conn.execute("CREATE TABLE IF NOT EXISTS claims (key TEXT PRIMARY KEY, created_at TEXT)")
    return conn


def add(number: str, role: str, content: str):
    with _connect() as conn:
        conn.execute("INSERT INTO messages (number, role, content, created_at) VALUES (?, ?, ?, ?)",
                     (number, role, content, datetime.datetime.now(datetime.timezone.utc).isoformat()))


def history(number: str, turns: int = 6) -> list:
    """The last few messages as {"role", "content"} dicts, oldest first."""
    with _connect() as conn:
        rows = conn.execute("SELECT role, content FROM messages WHERE number = ? ORDER BY id DESC LIMIT ?",
                            (number, turns * 2)).fetchall()
    return [{"role": role, "content": content} for role, content in reversed(rows)]


def claim(key: str) -> bool:
    """True the first time a key is claimed, False after that, even across processes. Used so a
    payment that is reported twice (redirect and webhook) is only recorded once."""
    with _connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO claims (key, created_at) VALUES (?, ?)",
                           (key, datetime.datetime.now(datetime.timezone.utc).isoformat()))
        return cur.rowcount == 1


def unclaim(key: str):
    with _connect() as conn:
        conn.execute("DELETE FROM claims WHERE key = ?", (key,))
