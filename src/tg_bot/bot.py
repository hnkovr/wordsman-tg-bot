"""aiogram v3 Telegram bot: thin Telegram I/O over the wordlists FastAPI service."""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    Document,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from tg_bot import artifacts, menu, pipeline, rusearch, scope
from tg_bot.config import Settings, get_settings
from tg_bot.logger import log
from tg_bot.menu import Row
from tg_bot.notify import ServiceNotifier, describe_user
from tg_bot.picks import Pick, get_picks
from tg_bot.scope import ScopeMiddleware
from tg_bot.store import get_store

router = Router()

HELP_TEXT = (
    "Send me a movie title (optionally with a year, e.g. `Dune 2021`) and I will "
    "fetch its subtitles and reply with vocabulary wordlists in every supported "
    "format (Anki, Quizlet, Mochi, Obsidian, …).\n\n"
    "Or send a document — .pdf, .html, .srt, .vtt, .txt, .md — and I will build "
    "the wordlists from its text instead.\n\n"
    "Commands:\n"
    "• /ru <movie> — find Russian subtitles and audio tracks\n"
    "• /ru_subs <movie> · /ru_audio <movie> — subtitles-only / audio-only RU search\n"
    "  (results come back as buttons — tap one and I send the subtitle file itself)\n"
    "• /en_audio <movie> — find English audio tracks\n"
    "• /orig_audio <movie> — find the original (untranslated) audio track\n"
    "• /files — send already-generated subtitles, wordlists or audio\n"
    "• /settings — adjust your level, word count, formats and search language\n"
    "  (Search language: RU makes plain messages run the RU search instead)\n"
    "• /reset — restore default settings\n"
    "• /help — show this message"
)

#: The "/" command menu shown in Telegram clients.
BOT_COMMANDS = [
    BotCommand(command="ru", description="Find Russian subtitles and audio for a movie"),
    BotCommand(command="ru_subs", description="Find Russian subtitles for a movie"),
    BotCommand(command="ru_audio", description="Find Russian audio tracks for a movie"),
    BotCommand(command="en_audio", description="Find English audio tracks for a movie"),
    BotCommand(command="orig_audio", description="Find the original audio track for a movie"),
    BotCommand(command="files", description="Send available subtitles, wordlists or audio"),
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


def _result_markup(rows: list[list[rusearch.Button]]) -> InlineKeyboardMarkup | None:
    """Search-result keyboard: callback buttons deliver files, url buttons open pages."""
    if not rows:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=button.label, callback_data=button.data)
                if button.data
                else InlineKeyboardButton(text=button.label, url=button.url)
                for button in row
            ]
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


@router.message(Command("files"))
async def on_files(message: Message) -> None:
    text, rows = menu.files_view(get_settings())
    await message.answer(text, reply_markup=_markup(rows))


_RU_MODE_LABEL = {"both": "сабы и аудио", "subs": "субтитры", "audio": "аудио-дорожки"}


async def _search_flow(
    message: Message, bot: Bot | None, query: str, mode: str, lang: str = "ru"
) -> None:
    """Run the search legs concurrently and reply with one HTML report.

    `mode` picks the sections (subtitles / audio / both), `lang` the audio language —
    "ru", "en", or "original" (the untranslated track). Subtitles stay Russian-only.
    """
    settings = get_settings()
    notifier = ServiceNotifier(bot, settings)
    who = describe_user(message)
    title, year = pipeline.parse_movie_query(query)
    if not title:
        await message.answer(
            "Usage: /ru <movie title> [year] — e.g. /ru Dune 2021\n"
            "Also: /ru_subs (subtitles only), /ru_audio (audio only),\n"
            "/en_audio (English audio), /orig_audio (original audio)."
        )
        return
    shown = f"{title} ({year})" if year else title
    lang_label = rusearch.LANG_LABELS[lang][2]
    await notifier.send(f"🔎 {who} ищет {lang_label} {_RU_MODE_LABEL[mode]} для “{shown}”")
    await message.answer(
        f"Ищу {lang_label} {_RU_MODE_LABEL[mode]} для {shown} — может занять минуту…"
    )

    want_subs = mode in ("both", "subs")
    want_audio = mode in ("both", "audio")
    jobs = []
    if want_subs:
        jobs.append(("local_subs", rusearch.local_scan(settings, "subs")))
        jobs.append(("online_subs", rusearch.online_subs(title, year, settings)))
    if want_audio:
        jobs.append(("local_audio", rusearch.local_scan(settings, "audio", lang)))
        jobs.append(("online_audio", rusearch.online_audio(title, year, settings, lang)))
    raw = await asyncio.gather(*(coro for _, coro in jobs), return_exceptions=True)
    outcome: dict[str, object] = {}
    for (name, _), value in zip(jobs, raw, strict=True):
        if isinstance(value, BaseException):
            # One failed leg must never lose the other sections' results.
            log.error("ru {} leg failed for “{}”: {}", name, shown, value)
            reason = f"{type(value).__name__}: {value}"
            value = rusearch.LegResult(reason=reason) if name.startswith("online") else []
        outcome[name] = value

    picks = rusearch.collect_picks(
        mode=mode,
        local_subs=outcome.get("local_subs") or [],
        local_audio=outcome.get("local_audio") or [],
        online_subs_result=outcome.get("online_subs"),
        limit=settings.ru_limit,
    )
    # The buttons carry only `<session>:<index>` — 64 bytes of callback_data can hold
    # neither a candidate id nor a path, so the results themselves live in the store.
    session_id = get_picks(settings).save(_user_id(message) or 0, shown, picks) if picks else 0
    report, rows = rusearch.render_results(
        title,
        year,
        mode=mode,
        lang=lang,
        session_id=session_id,
        picks=picks,
        local_subs=outcome.get("local_subs") or [],
        local_audio=outcome.get("local_audio") or [],
        online_subs_result=outcome.get("online_subs"),
        online_audio_result=outcome.get("online_audio"),
        subs_sources=rusearch.load_sources(settings, "subs") if want_subs else [],
        audio_sources=rusearch.load_sources(settings, "audio") if want_audio else [],
        limit=settings.ru_limit,
    )
    await message.answer(
        report,
        parse_mode="HTML",
        reply_markup=_result_markup(rows),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    await notifier.send(_truncate(f"✅ {who}: поиск ({lang_label}) по “{shown}” выполнен"))


@router.message(Command("ru"))
async def on_ru(message: Message, command: CommandObject, bot: Bot | None = None) -> None:
    await _search_flow(message, bot, command.args or "", mode="both")


@router.message(Command("ru_subs"))
async def on_ru_subs(message: Message, command: CommandObject, bot: Bot | None = None) -> None:
    await _search_flow(message, bot, command.args or "", mode="subs")


@router.message(Command("ru_audio"))
async def on_ru_audio(message: Message, command: CommandObject, bot: Bot | None = None) -> None:
    await _search_flow(message, bot, command.args or "", mode="audio")


@router.message(Command("en_audio"))
async def on_en_audio(message: Message, command: CommandObject, bot: Bot | None = None) -> None:
    await _search_flow(message, bot, command.args or "", mode="audio", lang="en")


@router.message(Command("orig_audio"))
async def on_orig_audio(message: Message, command: CommandObject, bot: Bot | None = None) -> None:
    await _search_flow(message, bot, command.args or "", mode="audio", lang="original")


async def _deliver_pick(callback: CallbackQuery, pick: Pick, settings: Settings) -> None:
    """Send one tapped search result as a real file, or say why it cannot be sent."""
    if pick.kind == "local":
        path = rusearch.resolve_local_path(settings, pick.path)
        if path is None:
            await callback.message.answer(f"⚠️ Файла больше нет на диске: {pick.path}")
            return
        size = path.stat().st_size
        if size > artifacts.MAX_SEND_BYTES:
            # Embedded tracks live inside the whole movie file — naming the path is the
            # only useful answer, since Telegram will never carry it.
            await callback.message.answer(
                f"⚠️ {path.name} — {size // (1024 * 1024)} МБ, "
                f"больше лимита Telegram. Путь на диске:\n{path}"
            )
            return
        await callback.message.answer_document(FSInputFile(path), caption=path.name)
        return
    with tempfile.TemporaryDirectory(prefix="tg-bot-ru-") as tmp:
        saved = await rusearch.download_online_sub(
            settings, pick.provider, pick.candidate_id, Path(tmp), pick.file_name
        )
        await callback.message.answer_document(
            FSInputFile(saved), caption=f"{saved.name} — {pick.provider}"
        )


async def _send_picks(callback: CallbackQuery, data: str, settings: Settings) -> None:
    """`d:<session>:<index>` sends one result; `da:<session>` sends every subtitle.

    The callback is answered BEFORE the work starts: a download runs up to
    `ru_search_timeout` (120 s), far past the few seconds Telegram gives a callback
    query, and an unanswered button spins and then errors in the client.
    """
    parts = data.split(":")
    user = callback.from_user
    try:
        session_id = int(parts[1])
    except (IndexError, ValueError):
        await callback.answer("Не понимаю эту кнопку.", show_alert=True)
        return
    picks = get_picks(settings).load(session_id, user.id if user else 0)
    if not picks:
        await callback.answer(
            "Результаты этого поиска устарели — повторите поиск.", show_alert=True
        )
        return
    if parts[0] == "da":
        chosen = [pick for pick in picks if pick.kind == "online_sub"]
    else:
        try:
            chosen = [picks[int(parts[2])]]
        except (IndexError, ValueError):
            await callback.answer("Этого варианта больше нет — повторите поиск.", show_alert=True)
            return
    await callback.answer("Готовлю файл…" if len(chosen) == 1 else "Готовлю файлы…")
    for pick in chosen:
        try:
            await _deliver_pick(callback, pick, settings)
        except Exception as exc:  # one dead candidate must not kill the rest
            log.warning("delivery failed for {}: {}", pick.label, exc)
            await callback.message.answer(_truncate(f"⚠️ {pick.label}\n{exc}"))


async def _send_artifact(callback: CallbackQuery, data: str, settings: Settings) -> None:
    """Handle a `g:<code>:<index>` callback: send the chosen on-disk file."""
    try:
        _, code, index = data.split(":")
        kind = menu.KIND_LABELS[code][2]
        item = artifacts.available(settings, kind)[int(index)]
    except (ValueError, KeyError, IndexError):
        await callback.answer("That file is no longer available.", show_alert=True)
        return
    if item.size > artifacts.MAX_SEND_BYTES:
        mb = item.size // (1024 * 1024)
        await callback.answer(f"Too large to send over Telegram ({mb} MB).", show_alert=True)
        return
    await callback.message.answer_document(
        FSInputFile(item.path), caption=f"{item.slug} — {item.path.name}"
    )
    await callback.answer("Sent ✓")


@router.callback_query(F.data.startswith(("m:", "s:", "t:", "g:", "d:", "da:")))
async def on_menu_callback(callback: CallbackQuery) -> None:
    settings = get_settings()
    data = callback.data or ""
    if data.startswith("g:"):
        await _send_artifact(callback, data, settings)
        return
    if data.startswith(("d:", "da:")):
        await _send_picks(callback, data, settings)
        return
    user = callback.from_user
    text, rows = menu.handle_callback(
        get_store(settings), user.id, user.username or "", data, settings
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
    prefs = get_store(settings).get(_user_id(message) or 0)
    if prefs.language == "ru":
        # Search-language RU: plain titles run the RU subs/audio search instead
        # of the EN wordlist flow (see /settings → Search language).
        await _search_flow(message, bot, message.text or "", mode="both")
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


def build_dispatcher(settings: Settings) -> Dispatcher:
    """The update pipeline shared by both transports — long polling and webhook."""
    dp = Dispatcher()
    # Outer middleware: out-of-scope updates are dropped before any handler, filter or
    # service-chat notification runs. Registered on both update types the bot handles.
    guard = ScopeMiddleware(settings)
    dp.message.outer_middleware(guard)
    dp.callback_query.outer_middleware(guard)
    dp.include_router(router)
    return dp


async def run_bot() -> None:  # pragma: no cover - real Telegram polling loop
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    bot = Bot(token=settings.telegram_bot_token)
    dp = build_dispatcher(settings)
    # Polling and webhook are mutually exclusive per token: getUpdates fails while a
    # webhook is registered. Say which deployment owns it instead of dying on a 409.
    hook = await bot.get_webhook_info()
    if hook.url:
        raise SystemExit(
            f"a webhook is registered at {hook.url} — that deployment owns this token.\n"
            "Talk to it instead, or run `tg-bot webhook delete` to take polling back."
        )
    me = await bot.get_me()
    await bot.set_my_commands(BOT_COMMANDS)
    notifier = ServiceNotifier(bot, settings)
    where = scope.describe_scope(settings)
    await notifier.send(f"🟢 wordsman bot @{me.username} started (api={settings.api_url})\n{where}")
    log.info(
        "starting Telegram polling as @{}; api_url={}; scope: {}",
        me.username,
        settings.api_url,
        where,
    )
    # Being scoped to a topic the bot cannot read is a silent failure, so say it loudly.
    hint = scope.reading_hint(me.can_read_all_group_messages, settings)
    if hint:
        log.warning("cannot read group messages: {}", hint)
        await notifier.send(f"⚠️ {hint}")
    try:
        await dp.start_polling(bot)
    finally:
        await notifier.send(f"🔴 wordsman bot @{me.username} stopped")
