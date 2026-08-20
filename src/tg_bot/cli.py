"""Click CLI: serve the wordlists API or run the Telegram bot."""

from __future__ import annotations

import asyncio

import click
import uvicorn

from tg_bot import __version__
from tg_bot.config import get_settings


@click.group()
@click.version_option(version=__version__, prog_name="tg-bot")
def main() -> None:
    """Telegram bot + FastAPI service producing wordsman wordlists."""


@main.command()
@click.option("--host", default=None, help="Bind host (default: settings)")
@click.option("--port", default=None, type=int, help="Bind port (default: settings)")
@click.option("--reload", is_flag=True, default=False, help="Auto-reload for development")
def serve(host: str | None, port: int | None, reload: bool) -> None:  # pragma: no cover
    """Run the FastAPI wordlists service."""
    settings = get_settings()
    uvicorn.run(
        "tg_bot.api:app",
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
    )


@main.command()
def bot() -> None:  # pragma: no cover
    """Run the Telegram bot (long polling)."""
    from tg_bot.bot import run_bot

    asyncio.run(run_bot())


@main.group()
def webhook() -> None:
    """Inspect or change which deployment Telegram delivers updates to."""


async def _webhook_action(action: str) -> str:  # pragma: no cover - real Telegram calls
    from aiogram import Bot

    from tg_bot import webhook as hooks

    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    api = Bot(token=settings.telegram_bot_token)
    try:
        if action == "set":
            if not hooks.enabled(settings):
                raise SystemExit("TG_BOT_PUBLIC_URL is not set — nothing to point Telegram at")
            # force=True: asking for this by hand IS the deliberate takeover.
            return await hooks.TelegramWebhook(settings, bot=api).register(force=True)
        if action == "delete":
            await api.delete_webhook(drop_pending_updates=False)
            return "deleted — the token is free for long polling again"
        info = await api.get_webhook_info()
        return (
            f"url={info.url or '(none)'} pending={info.pending_update_count} "
            f"last_error={info.last_error_message or '(none)'}"
        )
    finally:
        await api.session.close()


@webhook.command("info")
def webhook_info() -> None:  # pragma: no cover
    """Show what Telegram currently has registered for this token."""
    click.echo(asyncio.run(_webhook_action("info")))


@webhook.command("set")
def webhook_set() -> None:  # pragma: no cover
    """Point Telegram at this deployment (TG_BOT_PUBLIC_URL + TG_BOT_WEBHOOK_PATH)."""
    click.echo(asyncio.run(_webhook_action("set")))


@webhook.command("delete")
def webhook_delete() -> None:  # pragma: no cover
    """Unregister the webhook so `tg-bot bot` (long polling) can run again."""
    click.echo(asyncio.run(_webhook_action("delete")))
