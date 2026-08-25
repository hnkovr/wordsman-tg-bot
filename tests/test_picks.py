"""The result cache behind the search keyboard: ownership, expiry and pruning."""

from __future__ import annotations

import time

import pytest

from tg_bot import picks as picksmod
from tg_bot.config import Settings
from tg_bot.picks import Pick, PickStore, get_picks
from tg_bot.store import resolve_db_path


@pytest.fixture
def pick_store(settings: Settings) -> PickStore:
    return PickStore(resolve_db_path(settings))


def _pick(label: str = "📄 Dune.ru.srt") -> Pick:
    return Pick(kind="online_sub", label=label, provider="subtitlecat", candidate_id="subs/1/x")


class TestRoundTrip:
    def test_saved_picks_come_back_intact(self, pick_store: PickStore) -> None:
        session = pick_store.save(7, "Dune 2021", [_pick(), _pick("📄 second.srt")])
        loaded = pick_store.load(session, 7)
        assert [p.label for p in loaded] == ["📄 Dune.ru.srt", "📄 second.srt"]
        assert loaded[0].candidate_id == "subs/1/x"

    def test_unicode_labels_survive(self, pick_store: PickStore) -> None:
        session = pick_store.save(7, "Интерстеллар", [_pick("📄 Интерстеллар.ru.srt")])
        assert pick_store.load(session, 7)[0].label == "📄 Интерстеллар.ru.srt"

    def test_sessions_do_not_collide(self, pick_store: PickStore) -> None:
        first = pick_store.save(7, "Dune", [_pick("a")])
        second = pick_store.save(7, "Arrival", [_pick("b")])
        assert first != second
        assert pick_store.load(first, 7)[0].label == "a"


class TestOwnership:
    def test_another_user_gets_nothing(self, pick_store: PickStore) -> None:
        """Session ids are sequential integers — guessable, so ownership is checked."""
        session = pick_store.save(7, "Dune", [_pick()])
        assert pick_store.load(session, 8) == []

    def test_unknown_session_is_empty_not_an_error(self, pick_store: PickStore) -> None:
        assert pick_store.load(999_999, 7) == []


class TestPruning:
    def test_expired_rows_are_dropped(self, pick_store: PickStore) -> None:
        stale = pick_store.save(7, "Old", [_pick()])
        with pick_store._connect() as conn:  # age the row past the TTL
            conn.execute(
                "UPDATE search_picks SET created_at = ? WHERE session_id = ?",
                (time.time() - picksmod.TTL_SECONDS - 1, stale),
            )
        pick_store.save(7, "New", [_pick()])  # any save prunes
        assert pick_store.load(stale, 7) == []

    def test_only_the_last_n_sessions_per_user_survive(
        self, pick_store: PickStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(picksmod, "MAX_SESSIONS_PER_USER", 3)
        sessions = [pick_store.save(7, f"q{i}", [_pick()]) for i in range(5)]
        assert pick_store.load(sessions[0], 7) == []
        assert pick_store.load(sessions[-1], 7) != []

    def test_pruning_is_per_user(
        self, pick_store: PickStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(picksmod, "MAX_SESSIONS_PER_USER", 1)
        mine = pick_store.save(7, "mine", [_pick()])
        for _ in range(3):
            pick_store.save(8, "theirs", [_pick()])
        assert pick_store.load(mine, 7) != []


class TestFactory:
    def test_get_picks_is_cached_per_db_path(self, settings: Settings) -> None:
        assert get_picks(settings) is get_picks(settings)

    def test_shares_the_prefs_database(self, settings: Settings) -> None:
        from tg_bot.store import get_store

        assert get_picks(settings).db_path == get_store(settings).db_path
