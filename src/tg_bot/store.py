"""SQLite-backed per-user preferences for multi-user mode.

Shared by the bot (writes via the /settings menu) and the API (reads to shape each
user's wordlists). A NULL column means "inherit the global default", so the store holds
only explicit overrides — a user who never touched settings has an all-NULL/absent row
and gets exactly the config.yml defaults.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from tg_bot.config import REPO_ROOT, Settings, get_settings

FMT_SEP = ","
#: Preference fields a user may override (each maps to a Settings field of the same name).
EDITABLE = ("min_level", "top", "formats_exclude")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id         INTEGER PRIMARY KEY,
    username        TEXT,
    min_level       TEXT,
    top             INTEGER,
    formats_exclude TEXT,
    updated_at      REAL NOT NULL
);
"""


@dataclass
class Prefs:
    """A user's stored overrides; None means "inherit the global default"."""

    user_id: int
    username: str = ""
    min_level: str | None = None
    top: int | None = None
    formats_exclude: list[str] | None = None
    updated_at: float | None = None


def resolve_db_path(settings: Settings) -> Path:
    path = Path(settings.db_path)
    return path if path.is_absolute() else REPO_ROOT / path


class PrefStore:
    """Thin sqlite3 wrapper. Safe across the separate bot and API processes (SQLite locks)."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def get(self, user_id: int) -> Prefs:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM user_prefs WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return Prefs(user_id=user_id)
        raw = row["formats_exclude"]
        return Prefs(
            user_id=row["user_id"],
            username=row["username"] or "",
            min_level=row["min_level"] or None,
            top=row["top"],
            formats_exclude=raw.split(FMT_SEP) if raw else None,
            updated_at=row["updated_at"],
        )

    def set(self, user_id: int, *, username: str | None = None, **fields: object) -> Prefs:
        """Upsert one or more override fields; unknown keys raise. Returns the merged Prefs."""
        unknown = set(fields) - set(EDITABLE)
        if unknown:
            raise ValueError(f"unknown pref field(s): {sorted(unknown)}")
        current = self.get(user_id)
        merged = {key: getattr(current, key) for key in EDITABLE}
        merged.update(fields)
        raw = merged["formats_exclude"]
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO user_prefs
                    (user_id, username, min_level, top, formats_exclude, updated_at)
                VALUES
                    (:user_id, :username, :min_level, :top, :formats_exclude, :updated_at)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    min_level = excluded.min_level,
                    top = excluded.top,
                    formats_exclude = excluded.formats_exclude,
                    updated_at = excluded.updated_at
                """,
                {
                    "user_id": user_id,
                    "username": username if username is not None else current.username,
                    "min_level": merged["min_level"] or None,
                    "top": merged["top"],
                    "formats_exclude": FMT_SEP.join(raw) if raw else None,
                    "updated_at": time.time(),
                },
            )
        return self.get(user_id)

    def reset(self, user_id: int) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM user_prefs WHERE user_id = ?", (user_id,))


@lru_cache
def _store_for(db_path: str) -> PrefStore:
    return PrefStore(db_path)


def get_store(settings: Settings | None = None) -> PrefStore:
    """Process-wide PrefStore for the configured DB path (one per path)."""
    settings = settings or get_settings()
    return _store_for(str(resolve_db_path(settings)))


def effective_settings(settings: Settings, prefs: Prefs) -> Settings:
    """A Settings copy with the user's non-NULL overrides applied (defaults otherwise)."""
    updates: dict[str, object] = {}
    if prefs.min_level is not None:
        updates["min_level"] = prefs.min_level
    if prefs.top is not None:
        updates["top"] = prefs.top
    if prefs.formats_exclude is not None:
        updates["formats_exclude"] = prefs.formats_exclude
    return settings.model_copy(update=updates) if updates else settings
