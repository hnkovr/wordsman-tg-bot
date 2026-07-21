"""aiogram v3 Telegram bot: thin Telegram I/O over the wordlists FastAPI service."""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, Document, Message

from tg_bot import pipeline
from tg_bot.config import Settings, get_settings
from tg_bot.logger import log

router = Router()

HELP_TEXT = (
    "Send me a movie title (optionally with a year, e.g. `Dune 2021`) and I will "
    "fetch its subtitles and reply with vocabulary wordlists in every supported "
    "format (Anki, Quizlet, Mochi, Obsidian, …).\n\n"
    "Or send a document — .pdf, .html, .srt, .vtt, .txt, .md — and I will build "
    "the wordlists from its text instead."
)


def _document_problem(doc: Document, settings: Settings) -> str | None:
    """Reason the document can't be processed, or None when it is acceptable."""
    name = doc.file_name or ""
    suffix = Path(name).suffix.lower()
    if suffix not in pipeline.SUPPORTED_SUFFIXES:
        allowed = ", ".join(sorted(pipeline.SUPPORTED_SUFFIXES))
        return f"I can't read '{suffix or name or 'that'}' files. Send one of: {allowed}"
    limit = int(settings.max_document_mb * 1024 * 1024)
    if doc.file_size and doc.file_size > limit:
        return f"That file is too large; the limit is {settings.max_document_mb:g} MB."
    return None


def _api_timeout(settings: Settings) -> float:
    return settings.fetch_timeout + settings.dict_timeout + 30.0


async def _post_movie(settings: Settings, title: str, year: int | None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=_api_timeout(settings)) as client:
        return await client.post(
            f"{settings.api_url}/api/v1/wordlists/movie",
            json={"title": title, "year": year},
        )


async def _post_document(settings: Settings, path: Path) -> httpx.Response:
    async with httpx.AsyncClient(timeout=_api_timeout(settings)) as client:
        with path.open("rb") as payload:
            return await client.post(
                f"{settings.api_url}/api/v1/wordlists/document",
                files={"file": (path.name, payload)},
            )


async def _reply_zip(message: Message, response: httpx.Response, fallback_name: str) -> None:
    filename = fallback_name
    disposition = response.headers.get("content-disposition", "")
    if "filename=" in disposition:
        filename = disposition.split("filename=")[-1].strip('" ')
    await message.answer_document(
        BufferedInputFile(response.content, filename=filename),
        caption="Your wordlists are ready — unzip and import into your study app.",
    )


def _api_error_text(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail", "")
    except ValueError:
        detail = ""
    return detail or f"The wordlist service failed (HTTP {response.status_code})."


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer("Hi! " + HELP_TEXT)


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(F.document)
async def on_document(message: Message, bot: Bot) -> None:
    settings = get_settings()
    doc = message.document
    problem = _document_problem(doc, settings)
    if problem:
        await message.answer(problem)
        return
    await message.answer(f"Got {doc.file_name} — building wordlists, this can take a while…")
    with tempfile.TemporaryDirectory(prefix="tg-bot-") as tmp:
        local = Path(tmp) / Path(doc.file_name).name
        await bot.download(doc, destination=str(local))
        response = await _post_document(settings, local)
    if response.status_code == 200:
        await _reply_zip(message, response, f"{Path(doc.file_name).stem}-wordlists.zip")
    else:
        await message.answer(_api_error_text(response))


@router.message(F.text)
async def on_text(message: Message) -> None:
    settings = get_settings()
    title, year = pipeline.parse_movie_query(message.text or "")
    if not title:
        await message.answer(HELP_TEXT)
        return
    shown = f"{title} ({year})" if year else title
    await message.answer(f"Searching subtitles for {shown} — this can take a few minutes…")
    response = await _post_movie(settings, title, year)
    if response.status_code == 200:
        await _reply_zip(message, response, f"{pipeline.slugify(shown)}-wordlists.zip")
    elif response.status_code == 404:
        await message.answer(
            f"I couldn't find subtitles for {shown}. Check the spelling/year, "
            "or send me the subtitles or a document directly."
        )
    else:
        await message.answer(_api_error_text(response))


async def run_bot() -> None:  # pragma: no cover - real Telegram polling loop
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    log.info("starting Telegram polling; api_url={}", settings.api_url)
    await dp.start_polling(bot)
