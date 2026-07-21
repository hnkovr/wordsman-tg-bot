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
