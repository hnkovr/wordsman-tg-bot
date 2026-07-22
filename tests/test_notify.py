"""Service-chat notification tests: link parsing, payload shape, failure containment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from tg_bot.config import Settings
from tg_bot.notify import ServiceNotifier, describe_user, parse_service_chat_url


class RecordingBot:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail = fail

    async def send_message(self, **kwargs: Any) -> None:
        if self._fail:
            raise RuntimeError("bot is not a member of that chat")
        self.calls.append(kwargs)


@dataclass
class FakeUser:
    id: int = 42
    username: str | None = "hnkovr"
    first_name: str | None = "Николай"
    last_name: str | None = None


@dataclass
class FakeMessage:
    from_user: FakeUser | None = None


class TestParseServiceChatUrl:
    def test_thread_link(self) -> None:
        assert parse_service_chat_url("https://t.me/c/2281796095/800") == (-1002281796095, 800)

    def test_chat_only_link(self) -> None:
        assert parse_service_chat_url("https://t.me/c/2281796095") == (-1002281796095, None)

    def test_public_link_rejected(self) -> None:
        with pytest.raises(ValueError, match="Not a private-chat"):
            parse_service_chat_url("https://t.me/somebot")


class TestServiceNotifier:
    def _settings(self, **overrides: Any) -> Settings:
        base = {"service_chat_id": -1002281796095, "service_thread_id": 800}
        return Settings(**{**base, **overrides})

    async def test_sends_with_thread(self) -> None:
        bot = RecordingBot()
        notifier = ServiceNotifier(bot, self._settings())
        assert notifier.enabled
        assert await notifier.send("hello") is True
        assert bot.calls == [{"chat_id": -1002281796095, "text": "hello", "message_thread_id": 800}]

    async def test_sends_without_thread(self) -> None:
        bot = RecordingBot()
        notifier = ServiceNotifier(bot, self._settings(service_thread_id=None))
        await notifier.send("hi")
        assert "message_thread_id" not in bot.calls[0]

    async def test_disabled_without_chat_id(self) -> None:
        notifier = ServiceNotifier(RecordingBot(), self._settings(service_chat_id=None))
        assert notifier.enabled is False
        assert await notifier.send("ignored") is False

    async def test_disabled_by_flag(self) -> None:
        notifier = ServiceNotifier(RecordingBot(), self._settings(service_notify=False))
        assert await notifier.send("ignored") is False

    async def test_disabled_without_bot(self) -> None:
        assert ServiceNotifier(None, self._settings()).enabled is False

    async def test_failure_is_contained(self) -> None:
        notifier = ServiceNotifier(RecordingBot(fail=True), self._settings())
        assert await notifier.send("boom") is False


class TestDescribeUser:
    def test_full(self) -> None:
        assert describe_user(FakeMessage(FakeUser())) == "@hnkovr (Николай, id=42)"

    def test_no_username(self) -> None:
        text = describe_user(FakeMessage(FakeUser(username=None, last_name="Крупий")))
        assert text.startswith("no-username (Николай Крупий")

    def test_missing_user(self) -> None:
        assert describe_user(FakeMessage()) == "unknown user"
