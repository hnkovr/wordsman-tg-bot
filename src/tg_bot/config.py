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
    # Confine group traffic to `service_thread_id` alone. Without it the bot answers in
    # every topic of the service chat (General included), which is never wanted in a
    # shared pet-project chat. Private chats are unaffected — end users still DM the bot.
    service_topic_only: bool = True

    # Subtitle providers forwarded to fetch_srt.sh, in priority order — the same set the
    # CLI/skill flow uses (subproducts/english-apps/apps_pipeline.yml search.providers):
    # gestdown covers TV episodes, yify + subtitlecat cover films. podnapisi is omitted on
    # purpose: its host is DNS-dead (wordsman#22). Override via TG_BOT_SRT_PROVIDERS.
    srt_providers: Annotated[list[str], NoDecode] = ["gestdown", "yify", "subtitlecat"]

    # Pipeline: parent wordsman checkout providing main.py + scripts/fetch_srt.sh.
    # None → auto-detected (submodule layout: <wordsman>/subproducts/tg-bot).
    wordsman_root: Path | None = None
    work_dir: Path = Path("work")

    # Repo cache: reuse the wordsman checkout's already-fetched subtitles (data/in/<slug>)
    # and already-built wordlists (data/out/<slug>) instead of re-fetching/re-generating.
    # Only usable where that data is actually on disk — the blobs live in Git-LFS, so a
    # deploy host without the LFS objects pulled must fall back to a live fetch. None →
    # auto-detect (<wordsman_root>/data). Set use_repo_cache=false to force live fetches.
    use_repo_cache: bool = True
    repo_data_dir: Path | None = None
    # Per-user preferences DB (multi-user mode); shared by the bot and the API.
    db_path: Path = Path("data/tg_bot.sqlite3")

    # Wordlist generation: every supported format minus this short exception list,
    # plus an except-words file dropped from the lists entirely.
    formats_exclude: Annotated[list[str], NoDecode] = ["sparsed-yaml", "sparsed-json"]
    except_list: Path | None = Path("config/except-words.txt")
    top: int = 200
    min_level: str = ""  # empty → wordsman conf/settings.yml default

    fetch_timeout: float = 240.0
    dict_timeout: float = 600.0
    max_document_mb: float = 20.0  # Telegram bot API download ceiling

    # --- RU search (/ru, /ru_subs, /ru_audio) ---
    # A second wordsman checkout providing the stdlib `wordsman.search` module
    # (main.py search-subs/search-audio --json) for the "already on disk" leg.
    # Distinct from wordsman_root, which must contain scripts/fetch_srt.sh and is
    # rejected otherwise. None → the local-scan leg is disabled.
    search_wordsman_root: Path | None = None
    # Directories scanned for on-disk RU subs/audio; empty → <wordsman_root>/data.
    ru_scan_dirs: Annotated[list[str], NoDecode] = []
    # Online RU subtitle providers passed to srt-search. subtitlecat is the only
    # ru-capable provider today; srt-search's own default (podnapisi) is DNS-dead.
    ru_subs_providers: Annotated[list[str], NoDecode] = ["subtitlecat"]
    ru_search_timeout: float = 120.0
    ru_limit: int = 5  # max items rendered per report section
    # Links-only source catalogs (dual_subtitle_sources / audio_sources). None →
    # auto-derive <wordsman_root>/subproducts/{srt-search,audio-search}/config/config.yml.
    ru_subs_sources_file: Path | None = None
    ru_audio_sources_file: Path | None = None

    @field_validator(
        "formats_exclude", "srt_providers", "ru_scan_dirs", "ru_subs_providers", mode="before"
    )
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
