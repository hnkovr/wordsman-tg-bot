"""Per-user preference store: overrides, inheritance, effective merge, isolation."""

from __future__ import annotations

import pytest

from tg_bot.config import Settings
from tg_bot.store import PrefStore, effective_settings


class TestPrefStore:
    def test_absent_user_inherits_all(self, store: PrefStore) -> None:
        prefs = store.get(42)
        assert prefs.user_id == 42
        assert prefs.min_level is None and prefs.top is None and prefs.formats_exclude is None

    def test_set_and_get_roundtrip(self, store: PrefStore) -> None:
        store.set(42, username="hnkovr", min_level="B1", top=100)
        prefs = store.get(42)
        assert (prefs.username, prefs.min_level, prefs.top) == ("hnkovr", "B1", 100)
        assert prefs.formats_exclude is None  # untouched field stays inherited

    def test_partial_update_preserves_other_fields(self, store: PrefStore) -> None:
        store.set(42, min_level="B2")
        store.set(42, top=300)
        prefs = store.get(42)
        assert prefs.min_level == "B2" and prefs.top == 300

    def test_formats_exclude_list_roundtrip(self, store: PrefStore) -> None:
        store.set(42, formats_exclude=["mochi", "anki"])
        assert store.get(42).formats_exclude == ["mochi", "anki"]

    def test_reset_clears(self, store: PrefStore) -> None:
        store.set(42, min_level="C1")
        store.reset(42)
        assert store.get(42).min_level is None

    def test_users_are_isolated(self, store: PrefStore) -> None:
        store.set(1, min_level="A1")
        store.set(2, min_level="C2")
        assert store.get(1).min_level == "A1"
        assert store.get(2).min_level == "C2"

    def test_unknown_field_rejected(self, store: PrefStore) -> None:
        with pytest.raises(ValueError, match="unknown pref field"):
            store.set(42, nope="x")

    def test_clearing_level_back_to_inherit(self, store: PrefStore) -> None:
        store.set(42, min_level="B1")
        store.set(42, min_level=None)
        assert store.get(42).min_level is None


class TestEffectiveSettings:
    def test_no_prefs_returns_same_object(self, settings: Settings, store: PrefStore) -> None:
        assert effective_settings(settings, store.get(42)) is settings

    def test_overrides_applied(self, settings: Settings, store: PrefStore) -> None:
        store.set(42, min_level="C1", top=50, formats_exclude=["mochi"])
        eff = effective_settings(settings, store.get(42))
        assert eff.min_level == "C1" and eff.top == 50 and eff.formats_exclude == ["mochi"]
        assert settings.top != 50  # original untouched


def test_get_store_is_cached_per_path(settings: Settings) -> None:
    from tg_bot.store import get_store

    assert get_store(settings) is get_store(settings)
