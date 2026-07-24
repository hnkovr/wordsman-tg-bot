"""Inline-keyboard settings menu — pure view + mutation logic, free of aiogram types.

A "view" returns (text, rows) where rows is a list of button-rows and each button is a
(label, callback_data) pair. bot.py converts rows to aiogram markup and routes callbacks
here. Keeping this aiogram-free makes the whole menu unit-testable without a bot.
"""

from __future__ import annotations

from tg_bot import pipeline
from tg_bot.config import Settings
from tg_bot.store import Prefs, PrefStore

Row = list[tuple[str, str]]
View = tuple[str, list[Row]]

LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]
TOP_CHOICES = [50, 100, 200, 300, 500]


def _level(prefs: Prefs, settings: Settings) -> str:
    return prefs.min_level or settings.min_level or "default"


def _top(prefs: Prefs, settings: Settings) -> int:
    return prefs.top if prefs.top is not None else settings.top


def _excluded(prefs: Prefs, settings: Settings) -> set[str]:
    source = (
        prefs.formats_exclude if prefs.formats_exclude is not None else settings.formats_exclude
    )
    return set(source)


def settings_text(prefs: Prefs, settings: Settings) -> str:
    excluded = _excluded(prefs, settings)
    enabled = [f for f in pipeline.SUPPORTED_FORMATS if f not in excluded]
    return (
        "⚙️ *Your settings*\n"
        f"• Level (min CEFR): {_level(prefs, settings)}\n"
        f"• Max words: {_top(prefs, settings)}\n"
        f"• Formats: {len(enabled)}/{len(pipeline.SUPPORTED_FORMATS)} enabled"
    )


def root_view(prefs: Prefs, settings: Settings) -> View:
    enabled = len(pipeline.SUPPORTED_FORMATS) - len(_excluded(prefs, settings))
    rows: list[Row] = [
        [(f"🎚 Level: {_level(prefs, settings)}", "m:level")],
        [(f"🔢 Max words: {_top(prefs, settings)}", "m:top")],
        [(f"🗂 Formats: {enabled} enabled", "m:formats")],
        [("♻️ Reset to defaults", "s:reset")],
    ]
    return settings_text(prefs, settings), rows


def _chunk(buttons: Row, per_row: int) -> list[Row]:
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def level_view(prefs: Prefs, settings: Settings) -> View:
    current = prefs.min_level or ""
    buttons = [(f"{'• ' if lv == current else ''}{lv}", f"s:level:{lv}") for lv in LEVELS]
    rows = _chunk(buttons, 3)
    rows.append([("Default (inherit)", "s:level:_"), ("‹ Back", "m:root")])
    return "Minimum CEFR level to keep:", rows


def top_view(prefs: Prefs, settings: Settings) -> View:
    current = _top(prefs, settings)
    buttons = [(f"{'• ' if n == current else ''}{n}", f"s:top:{n}") for n in TOP_CHOICES]
    rows = _chunk(buttons, 3)
    rows.append([("‹ Back", "m:root")])
    return "Maximum number of words per wordlist:", rows


def formats_view(prefs: Prefs, settings: Settings) -> View:
    excluded = _excluded(prefs, settings)
    buttons = [
        (f"{'✅' if f not in excluded else '⬜️'} {f}", f"t:fmt:{f}")
        for f in pipeline.SUPPORTED_FORMATS
    ]
    rows = _chunk(buttons, 2)
    rows.append([("‹ Back", "m:root")])
    return "Toggle the formats you want in your wordlists:", rows


_SUBMENUS = {"level": level_view, "top": top_view, "formats": formats_view, "root": root_view}


def handle_callback(
    store: PrefStore, user_id: int, username: str, data: str, settings: Settings
) -> View:
    """Apply a callback to the user's prefs and return the view to render next."""
    parts = data.split(":")
    kind = parts[0]

    if kind == "m":
        return _SUBMENUS.get(parts[1], root_view)(store.get(user_id), settings)

    if kind == "s" and parts[1] == "reset":
        store.reset(user_id)
        return root_view(store.get(user_id), settings)

    if kind == "s" and parts[1] == "level":
        value = parts[2]
        store.set(user_id, username=username, min_level=None if value == "_" else value)
        return level_view(store.get(user_id), settings)

    if kind == "s" and parts[1] == "top":
        store.set(user_id, username=username, top=int(parts[2]))
        return top_view(store.get(user_id), settings)

    if kind == "t" and parts[1] == "fmt":
        fmt = parts[2]
        excluded = _excluded(store.get(user_id), settings)
        excluded = excluded - {fmt} if fmt in excluded else excluded | {fmt}
        # Never let a user exclude every format — resolve_formats would then fail.
        if len(excluded) < len(pipeline.SUPPORTED_FORMATS):
            store.set(user_id, username=username, formats_exclude=sorted(excluded))
        return formats_view(store.get(user_id), settings)

    return root_view(store.get(user_id), settings)
