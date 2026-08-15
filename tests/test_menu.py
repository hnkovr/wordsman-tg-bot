"""Settings-menu views and callback mutations (aiogram-free)."""

from __future__ import annotations

from tg_bot import menu, pipeline
from tg_bot.config import Settings
from tg_bot.store import PrefStore


def _labels(rows: list[list[tuple[str, str]]]) -> list[str]:
    return [label for row in rows for label, _ in row]


def _datas(rows: list[list[tuple[str, str]]]) -> list[str]:
    return [data for row in rows for _, data in row]


class TestViews:
    def test_root_shows_current_values(self, settings: Settings, store: PrefStore) -> None:
        store.set(1, min_level="B1", top=100)
        text, rows = menu.root_view(store.get(1), settings)
        assert "B1" in text and "100" in text
        assert "m:level" in _datas(rows) and "s:reset" in _datas(rows)

    def test_level_view_marks_current(self, settings: Settings, store: PrefStore) -> None:
        store.set(1, min_level="B2")
        _text, rows = menu.level_view(store.get(1), settings)
        assert any(lbl.startswith("• B2") for lbl in _labels(rows))

    def test_formats_view_marks_enabled_and_excluded(
        self, settings: Settings, store: PrefStore
    ) -> None:
        _text, rows = menu.formats_view(store.get(1), settings)
        labels = _labels(rows)
        assert any("✅ anki" in lbl for lbl in labels)  # enabled by default
        assert any("⬜️ sparsed-yaml" in lbl for lbl in labels)  # excluded by default

    def test_root_shows_language_row(self, settings: Settings, store: PrefStore) -> None:
        text, rows = menu.root_view(store.get(1), settings)
        assert "Search language: EN" in text
        assert any("Search language: EN" in lbl for lbl in _labels(rows))
        assert "m:lang" in _datas(rows)

    def test_lang_view_marks_current(self, settings: Settings, store: PrefStore) -> None:
        store.set(1, language="ru")
        _text, rows = menu.lang_view(store.get(1), settings)
        assert any(lbl.startswith("• RU") for lbl in _labels(rows))


class TestFilesMenu:
    def test_files_view_has_three_separate_items(self, settings: Settings) -> None:
        _text, rows = menu.files_view(settings)
        datas = _datas(rows)
        assert {"m:files:sub", "m:files:wl", "m:files:au"} <= set(datas)
        assert "m:root" in datas  # back button

    def test_files_list_empty(self, settings: Settings) -> None:
        text, rows = menu.files_list_view(settings, "sub")
        assert "No subtitles" in text
        assert _datas(rows) == ["m:files"]

    def test_files_list_populated(self, settings: Settings) -> None:
        wl = settings.work_dir / "dune" / "dune-wordlists.zip"
        wl.parent.mkdir(parents=True)
        wl.write_bytes(b"zip")
        text, rows = menu.files_list_view(settings, "wl")
        assert "Available wordlists (1)" in text
        assert any(d.startswith("g:wl:") for d in _datas(rows))

    def test_callback_opens_files_menu(self, settings: Settings, store: PrefStore) -> None:
        text, _rows = menu.handle_callback(store, 1, "u", "m:files", settings)
        assert "pick a type" in text

    def test_callback_opens_files_list(self, settings: Settings, store: PrefStore) -> None:
        text, _rows = menu.handle_callback(store, 1, "u", "m:files:sub", settings)
        assert "subtitles" in text.lower()


class TestCallbacks:
    def test_navigate_to_submenu(self, settings: Settings, store: PrefStore) -> None:
        text, _rows = menu.handle_callback(store, 1, "u", "m:level", settings)
        assert "CEFR" in text

    def test_set_level(self, settings: Settings, store: PrefStore) -> None:
        menu.handle_callback(store, 1, "u", "s:level:C1", settings)
        assert store.get(1).min_level == "C1"

    def test_clear_level(self, settings: Settings, store: PrefStore) -> None:
        store.set(1, min_level="C1")
        menu.handle_callback(store, 1, "u", "s:level:_", settings)
        assert store.get(1).min_level is None

    def test_set_top(self, settings: Settings, store: PrefStore) -> None:
        menu.handle_callback(store, 1, "u", "s:top:300", settings)
        assert store.get(1).top == 300

    def test_set_language_ru(self, settings: Settings, store: PrefStore) -> None:
        menu.handle_callback(store, 1, "u", "s:lang:ru", settings)
        assert store.get(1).language == "ru"

    def test_clear_language_back_to_inherit(self, settings: Settings, store: PrefStore) -> None:
        store.set(1, language="ru")
        menu.handle_callback(store, 1, "u", "s:lang:_", settings)
        assert store.get(1).language is None

    def test_toggle_format_off_then_on(self, settings: Settings, store: PrefStore) -> None:
        menu.handle_callback(store, 1, "u", "t:fmt:anki", settings)  # exclude anki
        assert "anki" in store.get(1).formats_exclude
        menu.handle_callback(store, 1, "u", "t:fmt:anki", settings)  # re-include
        assert "anki" not in store.get(1).formats_exclude

    def test_cannot_exclude_every_format(self, settings: Settings, store: PrefStore) -> None:
        store.set(1, formats_exclude=list(pipeline.SUPPORTED_FORMATS[:-1]))
        last = pipeline.SUPPORTED_FORMATS[-1]
        menu.handle_callback(store, 1, "u", f"t:fmt:{last}", settings)
        # the toggle that would exclude everything is refused
        assert last not in store.get(1).formats_exclude

    def test_reset(self, settings: Settings, store: PrefStore) -> None:
        store.set(1, min_level="C1", top=50)
        menu.handle_callback(store, 1, "u", "s:reset", settings)
        assert store.get(1).min_level is None and store.get(1).top is None
