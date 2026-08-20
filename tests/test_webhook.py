"""Webhook transport: enablement, secret-token gate, delivery and registration."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tg_bot import webhook
from tg_bot.api import create_app
from tg_bot.config import Settings

#: A minimal but real Telegram update — aiogram validates the payload shape.
UPDATE = {
    "update_id": 42,
    "message": {
        "message_id": 7,
        "date": 1_750_000_000,
        "chat": {"id": 5, "type": "private"},
        "from": {"id": 5, "is_bot": False, "first_name": "Tester"},
        "text": "/help",
    },
}


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeBot:
    """Records the Telegram API calls the webhook makes, without touching the network."""

    def __init__(self) -> None:
        self.session = FakeSession()
        self.set_webhook_calls: list[tuple[str, dict[str, Any]]] = []
        self.commands: list[Any] | None = None

    async def set_webhook(self, url: str, **kwargs: Any) -> bool:
        self.set_webhook_calls.append((url, kwargs))
        return True

    async def set_my_commands(self, commands: list[Any]) -> bool:
        self.commands = commands
        return True


class FakeDispatcher:
    def __init__(self, fail: bool = False) -> None:
        self.fed: list[Any] = []
        self.fail = fail

    def resolve_used_update_types(self) -> list[str]:
        return ["message", "callback_query"]

    async def feed_update(self, bot: Any, update: Any) -> None:
        self.fed.append(update)
        if self.fail:
            raise RuntimeError("handler exploded")


@pytest.fixture
def hook_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "telegram_bot_token": "424242:TESTTOKENTESTTOKENTESTTOKENTESTTOKEN",
            "public_url": "https://wordsman-tg-bot.fly.dev",
            "webhook_secret": "s3cret-token",
        }
    )


def make_hook(settings: Settings, *, fail: bool = False) -> webhook.TelegramWebhook:
    return webhook.TelegramWebhook(settings, bot=FakeBot(), dispatcher=FakeDispatcher(fail=fail))


@pytest.fixture
def hooked(
    hook_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> tuple[webhook.TelegramWebhook, Any]:
    """A create_app() wired to a fake bot/dispatcher instead of the real Telegram ones."""
    hook = make_hook(hook_settings)
    monkeypatch.setattr(webhook.TelegramWebhook, "build", classmethod(lambda cls, s: hook))
    return hook, create_app(hook_settings)


# --- enablement -------------------------------------------------------------------


def test_disabled_without_public_url(settings: Settings) -> None:
    assert not webhook.enabled(settings)
    assert webhook.TelegramWebhook.build(settings) is None


def test_disabled_without_token(hook_settings: Settings) -> None:
    """A standby with a URL but no token must not claim the webhook either."""
    assert not webhook.enabled(hook_settings.model_copy(update={"telegram_bot_token": ""}))


def test_standby_app_has_no_webhook_route(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.post("/tg/webhook", json=UPDATE).status_code == 404
        assert client.get("/healthz").json()["telegram"] == "off"


def test_url_joins_without_doubling_slashes(hook_settings: Settings) -> None:
    noisy = hook_settings.model_copy(
        update={"public_url": "https://example.org/", "webhook_path": "tg/hook/"}
    )
    assert webhook.webhook_url(noisy) == "https://example.org/tg/hook"
    assert webhook.webhook_path(noisy) == "/tg/hook"


# --- registration -----------------------------------------------------------------


def test_startup_registers_webhook_and_commands(hooked: tuple[Any, Any]) -> None:
    hook, app = hooked
    with TestClient(app):
        pass
    url, kwargs = hook.bot.set_webhook_calls[0]
    assert url == "https://wordsman-tg-bot.fly.dev/tg/webhook"
    assert kwargs["secret_token"] == "s3cret-token"
    assert kwargs["allowed_updates"] == ["message", "callback_query"]
    # Dropping the queue would discard messages sent while the machine was stopped.
    assert kwargs["drop_pending_updates"] is False
    assert hook.bot.commands, "the /-menu must be published on every cold start"
    assert hook.bot.session.closed, "the Telegram session must be released on shutdown"


def test_health_reports_webhook_mode(hooked: tuple[Any, Any]) -> None:
    _hook, app = hooked
    with TestClient(app) as client:
        assert client.get("/healthz").json()["telegram"] == "webhook"


# --- delivery ---------------------------------------------------------------------


def test_delivery_feeds_the_dispatcher(hooked: tuple[Any, Any]) -> None:
    hook, app = hooked
    with TestClient(app) as client:
        response = client.post(
            "/tg/webhook", json=UPDATE, headers={webhook.SECRET_HEADER: "s3cret-token"}
        )
        assert response.status_code == 200
    # Handlers run in the background, so the update lands by the time shutdown drains them.
    assert [update.update_id for update in hook.dispatcher.fed] == [42]


def test_wrong_secret_is_rejected(hooked: tuple[Any, Any]) -> None:
    hook, app = hooked
    with TestClient(app) as client:
        response = client.post(
            "/tg/webhook", json=UPDATE, headers={webhook.SECRET_HEADER: "forged"}
        )
        assert response.status_code == 403
        assert hook.dispatcher.fed == []


def test_missing_secret_header_is_rejected(hooked: tuple[Any, Any]) -> None:
    hook, app = hooked
    with TestClient(app) as client:
        assert client.post("/tg/webhook", json=UPDATE).status_code == 403
        assert hook.dispatcher.fed == []


def test_unset_secret_accepts_any_header(hook_settings: Settings, monkeypatch) -> None:
    """Local dev without a secret still works — the header is simply not checked."""
    open_settings = hook_settings.model_copy(update={"webhook_secret": ""})
    hook = make_hook(open_settings)
    monkeypatch.setattr(webhook.TelegramWebhook, "build", classmethod(lambda cls, s: hook))
    with TestClient(create_app(open_settings)) as client:
        assert client.post("/tg/webhook", json=UPDATE).status_code == 200
    assert len(hook.dispatcher.fed) == 1
    assert hook.bot.set_webhook_calls[0][1]["secret_token"] is None


def test_failing_handler_still_returns_200(hook_settings: Settings, monkeypatch) -> None:
    """A raising handler must not turn into a Telegram retry storm."""
    hook = make_hook(hook_settings, fail=True)
    monkeypatch.setattr(webhook.TelegramWebhook, "build", classmethod(lambda cls, s: hook))
    with TestClient(create_app(hook_settings)) as client:
        response = client.post(
            "/tg/webhook", json=UPDATE, headers={webhook.SECRET_HEADER: "s3cret-token"}
        )
        assert response.status_code == 200
    assert len(hook.dispatcher.fed) == 1  # fed, raised, swallowed — service still up
