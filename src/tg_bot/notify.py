"""Service-chat notifications: operational trace of bot activity for the maintainers.

End users talk to the bot directly; every request/result is mirrored as a short text
line into the configured service chat (a private supergroup, optionally a forum topic).
Notification failures are logged and swallowed — they must never break a user flow.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from tg_bot.config import Settings
from tg_bot.logger import log

#: https://t.me/c/<internal_id>/<thread_or_message_id> — the API chat id is -100<internal_id>.
_TME_C_RE = re.compile(r"t\.me/c/(?P<internal>\d+)(?:/(?P<thread>\d+))?")


def parse_service_chat_url(url: str) -> tuple[int, int | None]:
    """Turn a t.me/c/... link into (chat_id, thread_id) for the Bot API."""
    match = _TME_C_RE.search(url)
    if not match:
        raise ValueError(f"Not a private-chat Telegram link: {url!r}")
    chat_id = int(f"-100{match.group('internal')}")
    thread = match.group("thread")
    return chat_id, int(thread) if thread else None


class SupportsSendMessage(Protocol):
    async def send_message(self, **kwargs: Any) -> Any: ...


class ServiceNotifier:
    """Sends short operational messages to the service chat; a no-op when unconfigured."""

    def __init__(self, bot: SupportsSendMessage | None, settings: Settings) -> None:
        self._bot = bot
        self._chat_id = settings.service_chat_id
        self._thread_id = settings.service_thread_id
        self._enabled = bool(settings.service_notify and bot and settings.service_chat_id)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send(self, text: str) -> bool:
        """Post to the service chat. Returns True when delivered; never raises."""
        if not self._enabled:
            return False
        payload: dict[str, Any] = {"chat_id": self._chat_id, "text": text}
        if self._thread_id:
            payload["message_thread_id"] = self._thread_id
        try:
            await self._bot.send_message(**payload)
            return True
        except Exception as exc:  # noqa: BLE001 - notifications must never break a flow
            log.warning("service-chat notify failed: {}", exc)
            return False


def describe_user(message: Any) -> str:
    """Render '@handle (Full Name, id=123)' from a Telegram message, tolerating gaps."""
    user = getattr(message, "from_user", None)
    if user is None:
        return "unknown user"
    handle = f"@{user.username}" if getattr(user, "username", None) else "no-username"
    name = " ".join(
        part
        for part in (getattr(user, "first_name", None), getattr(user, "last_name", None))
        if part
    )
    parts = [handle]
    if name:
        parts.append(name)
    parts.append(f"id={getattr(user, 'id', '?')}")
    return f"{parts[0]} ({', '.join(parts[1:])})"
