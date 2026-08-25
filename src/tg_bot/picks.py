"""Short-lived storage for the results a user may tap on after a search.

A search reply is now a keyboard, and Telegram gives a button only 64 bytes of
`callback_data` — far too little for a provider candidate id, a file path or a title.
So the results are stored here and the button carries just `<session>:<index>`.

The rows are a cache, not a record: they are pruned by age and count, and a tap on a
result the store no longer holds is answered with "repeat the search", never with a
stale download. Rows also record their owner, so one user's session id cannot be used
from another user's chat.

Shares the SQLite file with `store.py` (the same per-deploy `data/tg_bot.sqlite3`) —
on the free hosts that file is wiped by a deploy, which is exactly the right lifetime.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

from tg_bot.config import Settings, get_settings
from tg_bot.logger import log
from tg_bot.store import resolve_db_path

#: A tapped button must still resolve minutes later (a Fly machine sleeps between
#: updates), but nothing here is worth keeping for days.
TTL_SECONDS = 24 * 3600
#: Per-user cap: a heavy searcher must not grow the table without bound.
MAX_SESSIONS_PER_USER = 20

_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_picks (
    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    query      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS search_picks_user ON search_picks (user_id, session_id);
"""


@dataclass(frozen=True)
class Pick:
    """One tappable search result.

    `local` carries a path already on disk; `online_sub` carries the provider plus the
    candidate id that srt-search needs to download exactly that subtitle.
    """

    kind: str  # "local" | "online_sub"
    label: str
    path: str = ""
    provider: str = ""
    candidate_id: str = ""
    file_name: str = ""


class PickStore:
    """Per-search result cache keyed by an auto-incrementing session id."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, user_id: int, query: str, picks: list[Pick]) -> int:
        """Store one search's results and return the session id the buttons carry."""
        payload = json.dumps([asdict(p) for p in picks], ensure_ascii=False)
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "INSERT INTO search_picks (user_id, query, payload, created_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, query, payload, time.time()),
            )
            session_id = int(cursor.lastrowid or 0)
            self._prune(conn, user_id)
        return session_id

    @staticmethod
    def _prune(conn: sqlite3.Connection, user_id: int) -> None:
        conn.execute("DELETE FROM search_picks WHERE created_at < ?", (time.time() - TTL_SECONDS,))
        conn.execute(
            "DELETE FROM search_picks WHERE user_id = ? AND session_id NOT IN "
            "(SELECT session_id FROM search_picks WHERE user_id = ? "
            " ORDER BY session_id DESC LIMIT ?)",
            (user_id, user_id, MAX_SESSIONS_PER_USER),
        )

    def load(self, session_id: int, user_id: int) -> list[Pick]:
        """The stored results, or [] when the session expired or belongs to someone else."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT user_id, payload FROM search_picks WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return []
        if row["user_id"] != user_id:
            log.warning(
                "pick session {} requested by user {} who does not own it", session_id, user_id
            )
            return []
        try:
            raw = json.loads(row["payload"])
        except ValueError:  # pragma: no cover - we wrote it ourselves
            return []
        return [Pick(**item) for item in raw]


@lru_cache
def _picks_for(db_path: str) -> PickStore:
    return PickStore(db_path)


def get_picks(settings: Settings | None = None) -> PickStore:
    """Process-wide PickStore for the configured DB path (one per path)."""
    settings = settings or get_settings()
    return _picks_for(str(resolve_db_path(settings)))
