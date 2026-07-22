"""Runtime settings; precedence: init kwargs > env > .env > config/config.yml > defaults."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_YML = REPO_ROOT / "config" / "config.yml"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TG_BOT_",
        # Later files win: config/.env is the canonical secrets file, root .env a fallback.
        env_file=(REPO_ROOT / ".env", REPO_ROOT / "config" / ".env"),
        yaml_file=CONFIG_YML,
        extra="ignore",
    )

    host: str = "0.0.0.0"  # noqa: S104 - service is meant to be reachable in docker
    port: int = 8340
    log_level: str = "INFO"

    # Bot-side: where the FastAPI service lives and how to authenticate to Telegram.
    api_url: str = "http://localhost:8340"
    telegram_bot_token: str = Field(
        "",
        validation_alias=AliasChoices("TELEGRAM_BOT_TOKEN", "TG_BOT_TELEGRAM_BOT_TOKEN"),
    )

    # Service chat: operational mirror of bot activity (end users still DM the bot).
    # chat_id/thread_id come from a t.me/c/<internal>/<thread> link, see notify.py.
    service_chat_id: int | None = None
    service_thread_id: int | None = None
    service_notify: bool = True

    # Subtitle providers forwarded to fetch_srt.sh, in priority order. yify is keyless and
    # healthy; podnapisi is a DNS-dead fallback (wordsman#22). Override via TG_BOT_SRT_PROVIDERS.
    srt_providers: Annotated[list[str], NoDecode] = ["yify", "podnapisi"]

    # Pipeline: parent wordsman checkout providing main.py + scripts/fetch_srt.sh.
    # None → auto-detected (submodule layout: <wordsman>/subproducts/tg-bot).
    wordsman_root: Path | None = None
    work_dir: Path = Path("work")

    # Wordlist generation: every supported format minus this short exception list,
    # plus an except-words file dropped from the lists entirely.
    formats_exclude: Annotated[list[str], NoDecode] = ["sparsed-yaml", "sparsed-json"]
    except_list: Path | None = Path("config/except-words.txt")
    top: int = 200
    min_level: str = ""  # empty → wordsman conf/settings.yml default

    fetch_timeout: float = 240.0
    dict_timeout: float = 600.0
    max_document_mb: float = 20.0  # Telegram bot API download ceiling

    @field_validator("formats_exclude", "srt_providers", mode="before")
    @classmethod
    def split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
