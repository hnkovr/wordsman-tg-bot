"""Loguru logger configured once from settings (TG_BOT_LOG_LEVEL)."""

from __future__ import annotations

import sys

from loguru import logger

from tg_bot.config import get_settings

logger.remove()
logger.add(
    sys.stderr,
    level=get_settings().log_level.upper(),
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan> - {message}"
    ),
)

log = logger
