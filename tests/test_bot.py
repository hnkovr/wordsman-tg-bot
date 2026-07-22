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
class FakeMessage:
    text: str | None = None
    document: FakeDocument | None = None
    answers: list[str] = field(default_factory=list)
    documents: list[tuple[str, bytes]] = field(default_factory=list)

    async def answer(self, text: str) -> None:
        self.answers.append(text)

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
        async def fake_post(settings: Settings, title: str, year: int | None) -> FakeResponse:
            assert (title, year) == ("Dune", 2021)
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

        async def fake_post(settings: Settings, path: Path) -> FakeResponse:
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


class TestTruncate:
    def test_short_untouched(self) -> None:
        assert botmod._truncate("hi") == "hi"

    def test_long_clipped_with_ellipsis(self) -> None:
        out = botmod._truncate("x" * 5000)
        assert len(out) == botmod._TG_LIMIT
        assert out.endswith("…")


def test_get_settings_cached() -> None:
    assert get_settings() is get_settings()
