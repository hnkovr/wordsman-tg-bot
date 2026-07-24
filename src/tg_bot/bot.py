"""aiogram v3 Telegram bot: thin Telegram I/O over the wordlists FastAPI service."""

from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from tg_bot import menu, pipeline
from tg_bot.config import Settings, get_settings
from tg_bot.logger import log
from tg_bot.menu import Row
from tg_bot.notify import ServiceNotifier, describe_user
from tg_bot.store import get_store

router = Router()

HELP_TEXT = (
    "Send me a movie title (optionally with a year, e.g. `Dune 2021`) and I will "
    "fetch its subtitles and reply with vocabulary wordlists in every supported "
    "format (Anki, Quizlet, Mochi, Obsidian, …).\n\n"
    "Or send a document — .pdf, .html, .srt, .vtt, .txt, .md — and I will build "
    "the wordlists from its text instead.\n\n"
    "Commands:\n"
    "• /settings — adjust your level, word count and formats\n"
    "• /reset — restore default settings\n"
    "• /help — show this message"
)

#: The "/" command menu shown in Telegram clients.
BOT_COMMANDS = [
    BotCommand(command="settings", description="Adjust your level, word count and formats"),
    BotCommand(command="reset", description="Restore default settings"),
    BotCommand(command="help", description="How to use this bot"),
]


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


async def _post_movie(
    settings: Settings, title: str, year: int | None, user_id: int | None
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=_api_timeout(settings)) as client:
        return await client.post(
            f"{settings.api_url}/api/v1/wordlists/movie",
            json={"title": title, "year": year, "user_id": user_id},
        )


async def _post_document(settings: Settings, path: Path, user_id: int | None) -> httpx.Response:
    data = {"user_id": str(user_id)} if user_id is not None else None
    async with httpx.AsyncClient(timeout=_api_timeout(settings)) as client:
        with path.open("rb") as payload:
            return await client.post(
                f"{settings.api_url}/api/v1/wordlists/document",
                files={"file": (path.name, payload)},
                data=data,
            )


def _markup(rows: list[Row]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in rows
        ]
    )


def _user_id(message: Message) -> int | None:
    return message.from_user.id if message.from_user else None


async def _reply_zip(message: Message, response: httpx.Response, fallback_name: str) -> None:
    filename = fallback_name
    disposition = response.headers.get("content-disposition", "")
    if "filename=" in disposition:
        filename = disposition.split("filename=")[-1].strip('" ')
    await message.answer_document(
        BufferedInputFile(response.content, filename=filename),
        caption="Your wordlists are ready — unzip and import into your study app.",
    )


#: Telegram rejects messages over 4096 chars; leave margin for our prefixes.
_TG_LIMIT = 4000


def _truncate(text: str, limit: int = _TG_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _api_error_text(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail", "")
    except (ValueError, AttributeError):
        detail = ""
    return str(detail) if detail else f"The wordlist service failed (HTTP {response.status_code})."


async def _report_failure(
    message: Message, notifier: ServiceNotifier, who: str, what: str, exc: Exception
) -> None:
    """Surface the real error text to the user and the service chat; never re-raise."""
    detail = f"{type(exc).__name__}: {exc}"
    log.exception("processing failed for {} ({})", who, what)
    with contextlib.suppress(Exception):
        await message.answer(_truncate(f"⚠️ Sorry, processing {what} failed:\n{detail}"))
    await notifier.send(_truncate(f"❌ {who}: {what} errored — {detail}"))


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer("Hi! " + HELP_TEXT)


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("settings"))
async def on_settings(message: Message) -> None:
    settings = get_settings()
    prefs = get_store(settings).get(_user_id(message) or 0)
    text, rows = menu.root_view(prefs, settings)
    await message.answer(text, reply_markup=_markup(rows), parse_mode="Markdown")


@router.message(Command("reset"))
async def on_reset(message: Message) -> None:
    settings = get_settings()
    store = get_store(settings)
    uid = _user_id(message) or 0
    store.reset(uid)
    text, rows = menu.root_view(store.get(uid), settings)
    await message.answer(
        "Settings reset to defaults.\n\n" + text,
        reply_markup=_markup(rows),
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith(("m:", "s:", "t:")))
async def on_menu_callback(callback: CallbackQuery) -> None:
    settings = get_settings()
    user = callback.from_user
    text, rows = menu.handle_callback(
        get_store(settings), user.id, user.username or "", callback.data or "", settings
    )
    with contextlib.suppress(Exception):  # ignore "message is not modified"
        await callback.message.edit_text(text, reply_markup=_markup(rows), parse_mode="Markdown")
    await callback.answer()


@router.message(F.document)
async def on_document(message: Message, bot: Bot) -> None:
    settings = get_settings()
    notifier = ServiceNotifier(bot, settings)
    doc = message.document
    who = describe_user(message)
    problem = _document_problem(doc, settings)
    if problem:
        await notifier.send(f"⛔️ {who} sent a rejected document {doc.file_name}: {problem}")
        await message.answer(problem)
        return
    await notifier.send(f"📄 {who} sent document {doc.file_name} — building wordlists")
    await message.answer(f"Got {doc.file_name} — building wordlists, this can take a while…")
    try:
        with tempfile.TemporaryDirectory(prefix="tg-bot-") as tmp:
            local = Path(tmp) / Path(doc.file_name).name
            await bot.download(doc, destination=str(local))
            response = await _post_document(settings, local, _user_id(message))
    except Exception as exc:  # e.g. download failure or API unreachable
        await _report_failure(message, notifier, who, doc.file_name, exc)
        return
    if response.status_code == 200:
        await _reply_zip(message, response, f"{Path(doc.file_name).stem}-wordlists.zip")
        await notifier.send(f"✅ {who}: wordlists delivered for {doc.file_name}")
    else:
        error = _api_error_text(response)
        await message.answer(_truncate(error))
        await notifier.send(_truncate(f"❌ {who}: {doc.file_name} failed — {error}"))


@router.message(F.text)
async def on_text(message: Message, bot: Bot | None = None) -> None:
    settings = get_settings()
    notifier = ServiceNotifier(bot, settings)
    who = describe_user(message)
    title, year = pipeline.parse_movie_query(message.text or "")
    if not title:
        await message.answer(HELP_TEXT)
        return
    shown = f"{title} ({year})" if year else title
    await notifier.send(f"🎬 {who} asked for “{shown}”")
    await message.answer(f"Searching subtitles for {shown} — this can take a few minutes…")
    try:
        response = await _post_movie(settings, title, year, _user_id(message))
    except Exception as exc:  # e.g. API unreachable
        await _report_failure(message, notifier, who, f"“{shown}”", exc)
        return
    if response.status_code == 200:
        await _reply_zip(message, response, f"{pipeline.slugify(shown)}-wordlists.zip")
        await notifier.send(f"✅ {who}: wordlists delivered for “{shown}”")
    elif response.status_code == 404:
        detail = _api_error_text(response)
        await message.answer(
            _truncate(
                f"I couldn't find subtitles for {shown}.\n{detail}\n\n"
                "Check the spelling/year, or send me the subtitles or a document directly."
            )
        )
        await notifier.send(_truncate(f"🔍 {who}: “{shown}” — {detail}"))
    else:
        error = _api_error_text(response)
        await message.answer(_truncate(error))
        await notifier.send(_truncate(f"❌ {who}: “{shown}” failed — {error}"))


async def run_bot() -> None:  # pragma: no cover - real Telegram polling loop
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    me = await bot.get_me()
    await bot.set_my_commands(BOT_COMMANDS)
    notifier = ServiceNotifier(bot, settings)
    await notifier.send(f"🟢 wordsman bot @{me.username} started (api={settings.api_url})")
    log.info("starting Telegram polling as @{}; api_url={}", me.username, settings.api_url)
    try:
        await dp.start_polling(bot)
    finally:
        await notifier.send(f"🔴 wordsman bot @{me.username} stopped")
