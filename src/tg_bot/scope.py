"""Which chats and forum topics the bot is allowed to act in.

Two separate concerns, often confused:

* **Where the bot answers** — decided here. Without a guard, adding the bot to any group
  makes it answer in every topic of that group. `in_scope()` narrows group traffic to the
  single configured `service_chat_id` + `service_thread_id`, while leaving private chats
  open (end users DM the bot directly).
* **Whether the bot even *receives* group messages** — decided by Telegram, not by us.
  A bot with privacy mode ON (BotFather's default) is delivered only commands, replies to
  itself and @mentions; plain text in a topic never reaches the handler. That is a
  BotFather setting (`/setprivacy` → Disable), and `getMe().can_read_all_group_messages`
  reports it — see `reading_hint()`.

Replies need no special handling: aiogram ≥3 fills `message_thread_id` from the incoming
message when `is_topic_message`, so an answer stays in the topic it came from.
"""

from __future__ import annotations

from typing import Any

from tg_bot.config import Settings
from tg_bot.logger import log

#: Chat types Telegram uses for group-like chats (as opposed to "private"/"channel").
GROUP_TYPES = frozenset({"group", "supergroup"})

#: Telegram reports the General topic as thread id 1, or omits it entirely.
GENERAL_TOPIC_IDS = frozenset({None, 1})


def in_scope(
    chat_id: int | None,
    chat_type: str | None,
    thread_id: int | None,
    settings: Settings,
) -> bool:
    """True when the bot should act on an update from this chat/topic.

    Private chats are always in scope. Group chats are in scope only when they are the
    configured service chat and — when a thread is configured and `service_topic_only`
    is set — the configured topic. Channels are never in scope.
    """
    if chat_type == "private":
        return True
    if chat_type not in GROUP_TYPES:
        return False
    if settings.service_chat_id is None or chat_id != settings.service_chat_id:
        return False
    if not settings.service_topic_only or settings.service_thread_id is None:
        return True
    # A message in the General topic carries no thread id (or 1), so it can never match a
    # configured topic — which is exactly the intent: one topic, not the whole group.
    return thread_id == settings.service_thread_id


def describe_scope(settings: Settings) -> str:
    """One line naming where the bot will answer — for logs, doctor and startup notices."""
    if settings.service_chat_id is None:
        return "private chats only (no service chat configured)"
    if settings.service_thread_id is None or not settings.service_topic_only:
        return f"private chats + all topics of chat {settings.service_chat_id}"
    return (
        f"private chats + chat {settings.service_chat_id} topic {settings.service_thread_id} only"
    )


def reading_hint(can_read_all_group_messages: bool | None, settings: Settings) -> str | None:
    """Warning text when the bot cannot actually read the topic it is scoped to, else None.

    With privacy mode ON the bot is scoped to a topic it will never receive plain
    messages from — the failure is silent, so it is worth saying out loud.
    """
    if settings.service_chat_id is None or can_read_all_group_messages:
        return None
    return (
        "privacy mode is ON, so Telegram delivers only commands, replies and @mentions "
        f"from chat {settings.service_chat_id} — plain messages in the topic will not "
        "reach the bot. Fix in BotFather: /setprivacy → select this bot → Disable, then "
        "remove and re-add the bot to the group for it to take effect."
    )


def _event_scope(event: Any) -> tuple[int | None, str | None, int | None]:
    """Extract (chat_id, chat_type, thread_id) from a Message or CallbackQuery."""
    message = getattr(event, "message", None) or event  # CallbackQuery carries .message
    chat = getattr(message, "chat", None)
    if chat is None:
        return None, None, None
    thread = getattr(message, "message_thread_id", None)
    if not getattr(message, "is_topic_message", False):
        thread = None
    return getattr(chat, "id", None), getattr(chat, "type", None), thread


class ScopeMiddleware:
    """aiogram outer middleware dropping updates from chats/topics outside the scope.

    Outer (not inner) so an out-of-scope update is discarded before any filter or handler
    runs — no reply, no service-chat notification, no pipeline work.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def __call__(self, handler: Any, event: Any, data: dict[str, Any]) -> Any:
        chat_id, chat_type, thread_id = _event_scope(event)
        if not in_scope(chat_id, chat_type, thread_id, self._settings):
            log.debug(
                "ignoring update from chat={} type={} thread={} (scope: {})",
                chat_id,
                chat_type,
                thread_id,
                describe_scope(self._settings),
            )
            return None
        return await handler(event, data)
