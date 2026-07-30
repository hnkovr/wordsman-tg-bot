"""Chat/topic scoping: where the bot answers, and whether it can read there at all."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from tg_bot import scope
from tg_bot.config import Settings

CHAT = -1002281796095
THREAD = 800
OTHER_CHAT = -1009999999999


@pytest.fixture
def scoped(settings: Settings) -> Settings:
    """Settings pinned to one chat + one topic, as the pet-project chat is configured."""
    return settings.model_copy(
        update={"service_chat_id": CHAT, "service_thread_id": THREAD, "service_topic_only": True}
    )


class TestInScope:
    def test_private_chat_always_allowed(self, scoped: Settings) -> None:
        assert scope.in_scope(12345, "private", None, scoped)

    def test_configured_topic_allowed(self, scoped: Settings) -> None:
        assert scope.in_scope(CHAT, "supergroup", THREAD, scoped)

    def test_other_topic_in_same_chat_rejected(self, scoped: Settings) -> None:
        assert not scope.in_scope(CHAT, "supergroup", THREAD + 1, scoped)

    def test_general_topic_rejected(self, scoped: Settings) -> None:
        """The General topic carries no thread id — it must not fall through as a match."""
        for general in scope.GENERAL_TOPIC_IDS:
            assert not scope.in_scope(CHAT, "supergroup", general, scoped)

    def test_other_group_rejected(self, scoped: Settings) -> None:
        assert not scope.in_scope(OTHER_CHAT, "supergroup", THREAD, scoped)

    def test_channel_rejected(self, scoped: Settings) -> None:
        assert not scope.in_scope(CHAT, "channel", THREAD, scoped)

    def test_groups_rejected_when_no_service_chat(self, settings: Settings) -> None:
        assert not scope.in_scope(CHAT, "supergroup", THREAD, settings)
        assert scope.in_scope(1, "private", None, settings)  # DMs still work

    def test_topic_only_disabled_allows_whole_chat(self, scoped: Settings) -> None:
        relaxed = scoped.model_copy(update={"service_topic_only": False})
        assert scope.in_scope(CHAT, "supergroup", THREAD + 5, relaxed)
        assert not scope.in_scope(OTHER_CHAT, "supergroup", THREAD, relaxed)

    def test_no_thread_configured_allows_whole_chat(self, scoped: Settings) -> None:
        no_thread = scoped.model_copy(update={"service_thread_id": None})
        assert scope.in_scope(CHAT, "supergroup", 42, no_thread)


class TestDescribeScope:
    def test_names_chat_and_topic(self, scoped: Settings) -> None:
        text = scope.describe_scope(scoped)
        assert str(CHAT) in text and str(THREAD) in text

    def test_private_only_when_unconfigured(self, settings: Settings) -> None:
        assert "private chats only" in scope.describe_scope(settings)


class TestReadingHint:
    def test_warns_when_privacy_mode_on(self, scoped: Settings) -> None:
        hint = scope.reading_hint(False, scoped)
        assert hint is not None
        assert "/setprivacy" in hint  # tells the operator exactly how to fix it

    def test_silent_when_bot_can_read(self, scoped: Settings) -> None:
        assert scope.reading_hint(True, scoped) is None

    def test_silent_without_service_chat(self, settings: Settings) -> None:
        """No group configured → privacy mode is irrelevant, so no noise."""
        assert scope.reading_hint(False, settings) is None


@dataclass
class FakeChat:
    id: int
    type: str


@dataclass
class FakeMessage:
    chat: FakeChat
    message_thread_id: int | None = None
    is_topic_message: bool = False


@dataclass
class FakeCallback:
    message: FakeMessage


class TestScopeMiddleware:
    async def _run(self, event: Any, settings: Settings) -> list[Any]:
        """Invoke the middleware, recording whether the handler was reached."""
        seen: list[Any] = []

        async def handler(evt: Any, data: dict[str, Any]) -> str:
            seen.append(evt)
            return "handled"

        result = await scope.ScopeMiddleware(settings)(handler, event, {})
        assert (result == "handled") == bool(seen)
        return seen

    async def test_passes_configured_topic(self, scoped: Settings) -> None:
        msg = FakeMessage(FakeChat(CHAT, "supergroup"), THREAD, is_topic_message=True)
        assert await self._run(msg, scoped) == [msg]

    async def test_drops_other_topic(self, scoped: Settings) -> None:
        msg = FakeMessage(FakeChat(CHAT, "supergroup"), THREAD + 1, is_topic_message=True)
        assert await self._run(msg, scoped) == []

    async def test_drops_general_topic(self, scoped: Settings) -> None:
        """No is_topic_message → General; must not inherit a stale thread id."""
        msg = FakeMessage(FakeChat(CHAT, "supergroup"), THREAD, is_topic_message=False)
        assert await self._run(msg, scoped) == []

    async def test_passes_private(self, scoped: Settings) -> None:
        msg = FakeMessage(FakeChat(777, "private"))
        assert await self._run(msg, scoped) == [msg]

    async def test_callback_uses_its_message_scope(self, scoped: Settings) -> None:
        good = FakeCallback(FakeMessage(FakeChat(CHAT, "supergroup"), THREAD, True))
        bad = FakeCallback(FakeMessage(FakeChat(OTHER_CHAT, "supergroup"), THREAD, True))
        assert await self._run(good, scoped) == [good]
        assert await self._run(bad, scoped) == []

    async def test_event_without_chat_is_dropped(self, scoped: Settings) -> None:
        assert await self._run(object(), scoped) == []
