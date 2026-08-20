"""Webhook mode: Telegram pushes updates into the FastAPI service.

Long polling needs a process that never sleeps. A webhook does not — which is what makes
the bot usable on a free/trial host whose machine stops when idle: Telegram's POST wakes
the machine (Fly `auto_start_machines`) and the bot answers from a cold start. Telegram
also enforces the singleton for us — registering a webhook disables `getUpdates` for the
token — so a webhook deployment and a poller can never fight over updates with 409s.

Enabled only when a public base URL *and* a token are configured, so a standby deployment
(Render) that leaves `TG_BOT_PUBLIC_URL` empty never claims the token from the active one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request, Response

from tg_bot.bot import BOT_COMMANDS, build_dispatcher
from tg_bot.config import Settings
from tg_bot.logger import log

#: Telegram echoes the configured `secret_token` back in this header on every delivery.
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def enabled(settings: Settings) -> bool:
    """True when this deployment should own the token's webhook."""
    return bool(settings.public_url and settings.telegram_bot_token)


def webhook_path(settings: Settings) -> str:
    return "/" + settings.webhook_path.strip("/")


def webhook_url(settings: Settings) -> str:
    return settings.public_url.rstrip("/") + webhook_path(settings)


class TelegramWebhook:
    """An aiogram bot+dispatcher pair wired into a FastAPI app as a webhook endpoint."""

    def __init__(
        self,
        settings: Settings,
        bot: Bot | None = None,
        dispatcher: Dispatcher | None = None,
    ) -> None:
        self.settings = settings
        self.bot = bot or Bot(token=settings.telegram_bot_token)
        self.dispatcher = dispatcher or build_dispatcher(settings)
        # Strong refs to in-flight handlers: asyncio only holds weak ones, so a task
        # nobody keeps can be garbage-collected mid-await and the reply never arrives.
        self._tasks: set[asyncio.Task[Any]] = set()
        #: URL this process registered, or None while another deployment owns the bot.
        self.registered: str | None = None

    @classmethod
    def build(cls, settings: Settings) -> TelegramWebhook | None:
        """The webhook for this deployment, or None when it is a standby."""
        return cls(settings) if enabled(settings) else None

    @property
    def path(self) -> str:
        return webhook_path(self.settings)

    @property
    def url(self) -> str:
        return webhook_url(self.settings)

    async def register(self, *, force: bool = False) -> str | None:
        """Point Telegram at this deployment, unless another one already owns the bot.

        Called on every cold start, so it must never steal: `setWebhook` silently wins,
        and a deployment that re-claimed on each wake would undo a failover the moment
        anything pinged its health endpoint (observed 2026-08-20). Claim only when the
        webhook is unset or already ours — taking over from a live owner is a deliberate
        act (`tg-bot webhook set`, or TG_BOT_WEBHOOK_FORCE_CLAIM).

        Re-sending our own registration stays unconditional: it is idempotent, and it is
        the only way to correct a rotated secret — `getWebhookInfo` never reveals one.

        Returns the registered URL, or None when another deployment owns the bot.
        """
        if not (force or self.settings.webhook_force_claim):
            info = await self.bot.get_webhook_info()
            if info.url and info.url != self.url:
                log.warning(
                    "not claiming the webhook: {} already owns this bot. Serving as a "
                    "standby — run `tg-bot webhook set` here to take over.",
                    info.url,
                )
                self.registered = None
                return None
        await self.bot.set_webhook(
            self.url,
            secret_token=self.settings.webhook_secret or None,
            allowed_updates=self.dispatcher.resolve_used_update_types(),
            # Never drop the queue: the machine is stopped between updates, so anything
            # pending is a real user message waiting to be answered, not stale backlog.
            drop_pending_updates=False,
        )
        await self.bot.set_my_commands(BOT_COMMANDS)
        log.info("webhook registered at {}", self.url)
        self.registered = self.url
        return self.url

    async def close(self) -> None:
        """Let in-flight handlers finish, then release the Telegram HTTP session.

        The webhook stays registered on Telegram's side on purpose: the machine stops
        between updates, and an unregistered webhook would mean a dead bot until the
        next deploy.
        """
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        await self.bot.session.close()

    def _spawn(self, update: Update) -> asyncio.Task[Any]:
        task = asyncio.create_task(self._handle(update))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _handle(self, update: Update) -> None:
        try:
            await self.dispatcher.feed_update(self.bot, update)
        except Exception:  # a failed handler must not take the service down
            log.exception("webhook update {} failed", update.update_id)

    def install(self, app: FastAPI) -> None:
        """Register the delivery endpoint on `app`."""

        @app.post(self.path, include_in_schema=False)
        async def telegram_webhook(request: Request) -> Response:
            secret = self.settings.webhook_secret
            if secret and request.headers.get(SECRET_HEADER) != secret:
                log.warning("rejected a webhook delivery with a bad secret token")
                raise HTTPException(status_code=403, detail="bad secret token")
            update = Update.model_validate(await request.json(), context={"bot": self.bot})
            # Answer Telegram now, work afterwards: a search runs for up to
            # `ru_search_timeout` seconds — far past Telegram's webhook read timeout —
            # and a timed-out delivery is retried, which would reply twice.
            self._spawn(update)
            return Response(status_code=200)

    @asynccontextmanager
    async def lifespan(self, app: FastAPI) -> AsyncIterator[None]:
        await self.register()
        try:
            yield
        finally:
            await self.close()
