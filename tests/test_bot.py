"""Bot handler tests with duck-typed fake Telegram objects and a fake HTTP layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tg_bot import bot as botmod
from tg_bot.config import Settings, get_settings


@dataclass
class FakeDocument:
    file_name: str = "story.txt"
    file_size: int = 1024


@dataclass
class FakeUser:
    id: int = 7
    username: str | None = "tester"


@dataclass
class FakeMessage:
    text: str | None = None
    document: FakeDocument | None = None
    from_user: FakeUser | None = field(default_factory=FakeUser)
    answers: list[str] = field(default_factory=list)
    documents: list[tuple[str, bytes]] = field(default_factory=list)
    markups: list[Any] = field(default_factory=list)

    async def answer(
        self,
        text: str,
        reply_markup: Any = None,
        parse_mode: str | None = None,
        link_preview_options: Any = None,
    ) -> None:
        self.answers.append(text)
        if reply_markup is not None:
            self.markups.append(reply_markup)

    async def answer_document(self, input_file: Any, caption: str = "") -> None:
        self.documents.append((input_file.filename, input_file.data))


class FakeBot:
    async def download(self, doc: FakeDocument, destination: str) -> None:
        Path(destination).write_bytes(b"downloaded words")


class FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", detail: str = "") -> None:
        self.status_code = status_code
        self.content = content
        self._detail = detail
        self.headers = {"content-disposition": 'attachment; filename="x-wordlists.zip"'}

    def json(self) -> dict[str, str]:
        if not self._detail:
            raise ValueError("no body")
        return {"detail": self._detail}


@pytest.fixture
def fixed_settings(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(botmod, "get_settings", lambda: settings)
    return settings


class TestHelpers:
    def test_document_problem_ok(self, settings: Settings) -> None:
        assert botmod._document_problem(FakeDocument(), settings) is None

    def test_document_problem_bad_type(self, settings: Settings) -> None:
        problem = botmod._document_problem(FakeDocument(file_name="a.docx"), settings)
        assert "can't read" in problem

    def test_document_problem_too_big(self, settings: Settings) -> None:
        big = FakeDocument(file_size=10**9)
        assert "too large" in botmod._document_problem(big, settings)

    def test_api_error_text_plain(self) -> None:
        assert "HTTP 500" in botmod._api_error_text(FakeResponse(500))

    def test_api_error_text_detail(self) -> None:
        assert botmod._api_error_text(FakeResponse(502, detail="boom")) == "boom"


class TestStartHelp:
    async def test_start(self) -> None:
        message = FakeMessage(text="/start")
        await botmod.on_start(message)
        assert "movie title" in message.answers[0]

    async def test_help(self) -> None:
        message = FakeMessage(text="/help")
        await botmod.on_help(message)
        assert message.answers == [botmod.HELP_TEXT]


class TestTextHandler:
    async def test_movie_success(
        self, fixed_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_post(
            settings: Settings, title: str, year: int | None, user_id: int | None
        ) -> FakeResponse:
            assert (title, year, user_id) == ("Dune", 2021, 7)
            return FakeResponse(200, content=b"zipbytes")

        monkeypatch.setattr(botmod, "_post_movie", fake_post)
        message = FakeMessage(text="Dune 2021")
        await botmod.on_text(message)
        assert message.documents == [("x-wordlists.zip", b"zipbytes")]

    async def test_movie_not_found_shows_real_reason(
        self, fixed_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_post(*args: Any) -> FakeResponse:
            return FakeResponse(404, detail="No subtitles found for 'X' — podnapisi DNS dead")

        monkeypatch.setattr(botmod, "_post_movie", fake_post)
        message = FakeMessage(text="X")
        await botmod.on_text(message)
        assert "couldn't find subtitles" in message.answers[-1]
        assert "podnapisi DNS dead" in message.answers[-1]  # real reason surfaced

    async def test_service_error(
        self, fixed_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_post(*args: Any) -> FakeResponse:
            return FakeResponse(502, detail="pipeline broke")

        monkeypatch.setattr(botmod, "_post_movie", fake_post)
        message = FakeMessage(text="Dune")
        await botmod.on_text(message)
        assert message.answers[-1] == "pipeline broke"

    async def test_api_unreachable_surfaces_exception_text(
        self, fixed_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_post(*args: Any) -> FakeResponse:
            raise ConnectionError("Connection refused to :8340")

        monkeypatch.setattr(botmod, "_post_movie", fake_post)
        message = FakeMessage(text="Dune")
        await botmod.on_text(message)
        assert "Connection refused to :8340" in message.answers[-1]
        assert "ConnectionError" in message.answers[-1]

    async def test_blank_text_shows_help(self, fixed_settings: Settings) -> None:
        message = FakeMessage(text="   ")
        await botmod.on_text(message)
        assert message.answers == [botmod.HELP_TEXT]


class TestDocumentHandler:
    async def test_document_success(
        self, fixed_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        posted: dict[str, bytes] = {}

        async def fake_post(settings: Settings, path: Path, user_id: int | None) -> FakeResponse:
            posted[path.name] = path.read_bytes()
            return FakeResponse(200, content=b"zipbytes")

        monkeypatch.setattr(botmod, "_post_document", fake_post)
        message = FakeMessage(document=FakeDocument())
        await botmod.on_document(message, FakeBot())
        assert posted == {"story.txt": b"downloaded words"}
        assert message.documents[0][1] == b"zipbytes"

    async def test_document_rejected_before_download(self, fixed_settings: Settings) -> None:
        message = FakeMessage(document=FakeDocument(file_name="a.exe"))
        await botmod.on_document(message, FakeBot())
        assert "can't read" in message.answers[0]
        assert message.documents == []

    async def test_document_service_error(
        self, fixed_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_post(*args: Any) -> FakeResponse:
            return FakeResponse(502, detail="no text")

        monkeypatch.setattr(botmod, "_post_document", fake_post)
        message = FakeMessage(document=FakeDocument())
        await botmod.on_document(message, FakeBot())
        assert message.answers[-1] == "no text"

    async def test_document_download_failure_surfaces_text(
        self, fixed_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class BrokenBot(FakeBot):
            async def download(self, doc: FakeDocument, destination: str) -> None:
                raise OSError("download timed out")

        async def fake_post(*args: Any) -> FakeResponse:  # should never be reached
            raise AssertionError("post must not run after a download failure")

        monkeypatch.setattr(botmod, "_post_document", fake_post)
        message = FakeMessage(document=FakeDocument())
        await botmod.on_document(message, BrokenBot())
        assert "download timed out" in message.answers[-1]
        assert "OSError" in message.answers[-1]


class TestSettingsMenu:
    async def test_settings_command_shows_keyboard(self, fixed_settings: Settings) -> None:
        message = FakeMessage(text="/settings")
        await botmod.on_settings(message)
        assert "Your settings" in message.answers[0]
        assert message.markups  # an inline keyboard was attached

    async def test_reset_command(self, fixed_settings: Settings) -> None:
        from tg_bot.store import get_store

        get_store(fixed_settings).set(7, min_level="C1")
        message = FakeMessage(text="/reset")
        await botmod.on_reset(message)
        assert get_store(fixed_settings).get(7).min_level is None
        assert "reset to defaults" in message.answers[0].lower()

    async def test_callback_sets_level(self, fixed_settings: Settings) -> None:
        from tg_bot.store import get_store

        @dataclass
        class FakeCallbackMessage:
            edits: list[str] = field(default_factory=list)

            async def edit_text(self, text: str, reply_markup: Any = None, **kw: Any) -> None:
                self.edits.append(text)

        @dataclass
        class FakeCallback:
            data: str
            from_user: FakeUser = field(default_factory=FakeUser)
            message: FakeCallbackMessage = field(default_factory=FakeCallbackMessage)
            answered: bool = False

            async def answer(self, *a: Any, **k: Any) -> None:
                self.answered = True

        cb = FakeCallback(data="s:level:B2")
        await botmod.on_menu_callback(cb)
        assert get_store(fixed_settings).get(7).min_level == "B2"
        assert cb.answered and cb.message.edits


class TestFilesMenu:
    async def test_files_command_shows_three_items(self, fixed_settings: Settings) -> None:
        message = FakeMessage(text="/files")
        await botmod.on_files(message)
        assert "pick a type" in message.answers[0]
        assert message.markups

    async def test_send_artifact_sends_file(self, fixed_settings: Settings) -> None:
        wl = fixed_settings.work_dir / "dune" / "dune-wordlists.zip"
        wl.parent.mkdir(parents=True)
        wl.write_bytes(b"zip-bytes")

        sent: list[Any] = []
        answered: list[str] = []

        @dataclass
        class CbMessage:
            async def answer_document(self, input_file: Any, caption: str = "") -> None:
                sent.append((input_file, caption))

        @dataclass
        class Cb:
            data: str
            message: CbMessage = field(default_factory=CbMessage)
            from_user: FakeUser = field(default_factory=FakeUser)

            async def answer(self, text: str = "", show_alert: bool = False) -> None:
                answered.append(text)

        await botmod.on_menu_callback(Cb(data="g:wl:0"))
        assert sent and sent[0][1].startswith("dune")  # caption names the item
        assert answered == ["Sent ✓"]

    async def test_send_artifact_gone(self, fixed_settings: Settings) -> None:
        alerts: list[tuple[str, bool]] = []

        @dataclass
        class Cb:
            data: str = "g:wl:0"
            from_user: FakeUser = field(default_factory=FakeUser)

            async def answer(self, text: str = "", show_alert: bool = False) -> None:
                alerts.append((text, show_alert))

        await botmod.on_menu_callback(Cb())  # no zip on disk → index error path
        assert alerts and alerts[0][1] is True
        assert "no longer available" in alerts[0][0].lower()


def test_bot_commands_are_telegram_legal() -> None:
    """Telegram allows only [a-z0-9_] in commands — hyphens would be rejected."""
    import re

    commands = [c.command for c in botmod.BOT_COMMANDS]
    assert {"ru", "ru_subs", "ru_audio"} <= set(commands)
    assert all(re.fullmatch(r"[a-z0-9_]{1,32}", c) for c in commands)


class TestRuSearch:
    @pytest.fixture
    def ru_ready(
        self, fixed_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> Settings:
        from tg_bot import rusearch

        async def fake_local(settings: Settings, kind: str) -> list[dict]:
            if kind == "subs":
                return [{"path": "x.ru.srt", "kind": "file", "confidence": 0.9}]
            return []

        async def fake_subs(title: str, year: int | None, settings: Settings):
            return rusearch.LegResult(
                items=[{"provider": "subtitlecat", "title": title, "year": year}]
            )

        async def fake_audio(title: str, year: int | None, settings: Settings):
            return rusearch.LegResult(reason="audio-search недоступен в этом деплое")

        monkeypatch.setattr(rusearch, "local_scan", fake_local)
        monkeypatch.setattr(rusearch, "online_subs", fake_subs)
        monkeypatch.setattr(rusearch, "online_audio", fake_audio)
        monkeypatch.setattr(
            rusearch,
            "load_sources",
            lambda settings, kind: [
                rusearch.SourceLink(
                    name="rutracker",
                    url="https://rutracker.org",
                    access="torrent",
                    search_url="https://rutracker.org/forum/tracker.php?nm={query}",
                )
            ],
        )
        return fixed_settings

    @staticmethod
    def _command(args: str | None) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(args=args)

    async def test_ru_command_full_report(self, ru_ready: Settings) -> None:
        message = FakeMessage(text="/ru Dune 2021")
        await botmod.on_ru(message, self._command("Dune 2021"))
        report = message.answers[-1]
        assert "Dune (2021)" in report
        for marker in ("📀", "🌐", "🔊", "🔗"):
            assert marker in report
        assert "rutracker.org/forum/tracker.php?nm=Dune+2021" in report
        assert "Торренты: только ссылки" in report

    async def test_ru_bare_shows_usage(self, ru_ready: Settings) -> None:
        message = FakeMessage(text="/ru")
        await botmod.on_ru(message, self._command(None))
        assert "Usage: /ru" in message.answers[-1]

    async def test_ru_subs_mode_has_no_audio_section(self, ru_ready: Settings) -> None:
        message = FakeMessage(text="/ru_subs Dune")
        await botmod.on_ru_subs(message, self._command("Dune"))
        report = message.answers[-1]
        assert "🌐" in report and "🔊" not in report

    async def test_ru_audio_mode_has_no_subs_section(self, ru_ready: Settings) -> None:
        message = FakeMessage(text="/ru_audio Dune")
        await botmod.on_ru_audio(message, self._command("Dune"))
        report = message.answers[-1]
        assert "🔊" in report and "🌐" not in report

    async def test_ru_pref_routes_plain_text_away_from_en_flow(
        self, ru_ready: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tg_bot.store import get_store

        async def explode(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("EN wordlist flow must not run for a RU-pref user")

        monkeypatch.setattr(botmod, "_post_movie", explode)
        get_store(ru_ready).set(7, language="ru")
        message = FakeMessage(text="Dune 2021")
        await botmod.on_text(message)
        assert "поиск RU" in message.answers[-1]

    async def test_one_failed_leg_keeps_other_sections(
        self, ru_ready: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tg_bot import rusearch

        async def boom(title: str, year: int | None, settings: Settings):
            raise RuntimeError("leg died")

        monkeypatch.setattr(rusearch, "online_subs", boom)
        message = FakeMessage(text="/ru Dune")
        await botmod.on_ru(message, self._command("Dune"))
        report = message.answers[-1]
        assert "RuntimeError: leg died" in report  # failed leg surfaced as its section
        assert "📀" in report and "🔊" in report  # other sections still rendered


class TestTruncate:
    def test_short_untouched(self) -> None:
        assert botmod._truncate("hi") == "hi"

    def test_long_clipped_with_ellipsis(self) -> None:
        out = botmod._truncate("x" * 5000)
        assert len(out) == botmod._TG_LIMIT
        assert out.endswith("…")


def test_get_settings_cached() -> None:
    assert get_settings() is get_settings()
