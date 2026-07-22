"""Unit tests for the pipeline helpers and the hermetic subprocess flows."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from tg_bot import pipeline
from tg_bot.config import Settings
from tg_bot.pipeline import PipelineError, SubtitlesNotFoundError


class TestParseMovieQuery:
    @pytest.mark.parametrize(
        ("raw", "title", "year"),
        [
            ("Dune", "Dune", None),
            ("Dune 2021", "Dune", 2021),
            ("Dune (2021)", "Dune", 2021),
            ("  The   Matrix  1999 ", "The Matrix", 1999),
            ("Blade Runner 2049", "Blade Runner 2049", None),
            ("Blade Runner 2049 (2017)", "Blade Runner 2049", 2017),
            ("1917", "1917", None),
        ],
    )
    def test_variants(self, raw: str, title: str, year: int | None) -> None:
        assert pipeline.parse_movie_query(raw) == (title, year)


class TestResolveFormats:
    def test_default_excludes_short_formats(self, settings: Settings) -> None:
        formats = pipeline.resolve_formats(settings).split(",")
        assert "sparsed-yaml" not in formats
        assert "sparsed-json" not in formats
        assert set(formats) < set(pipeline.SUPPORTED_FORMATS)
        assert "anki" in formats and "mochi" in formats

    def test_unknown_exclusion_rejected(self, settings: Settings) -> None:
        settings = settings.model_copy(update={"formats_exclude": ["nope"]})
        with pytest.raises(PipelineError, match="unknown formats"):
            pipeline.resolve_formats(settings)

    def test_everything_excluded_rejected(self, settings: Settings) -> None:
        settings = settings.model_copy(update={"formats_exclude": list(pipeline.SUPPORTED_FORMATS)})
        with pytest.raises(PipelineError, match="every supported format"):
            pipeline.resolve_formats(settings)


class TestTextExtraction:
    def test_html_to_text_strips_markup(self) -> None:
        html = (
            "<html><head><style>body{}</style><script>var x=1;</script></head>"
            "<body><h1>Title</h1><p>Hello &amp; welcome</p></body></html>"
        )
        text = pipeline.html_to_text(html)
        assert "Title" in text and "Hello & welcome" in text
        assert "var x" not in text and "body{}" not in text

    def test_txt_passthrough(self, tmp_path: Path) -> None:
        src = tmp_path / "story.txt"
        src.write_text("plain words", encoding="utf-8")
        dest = pipeline.extract_document_text(src, tmp_path / "prepared")
        assert dest.read_text(encoding="utf-8") == "plain words"
        assert dest.suffix == ".txt"

    def test_html_conversion(self, tmp_path: Path) -> None:
        src = tmp_path / "page.html"
        src.write_text("<p>web words</p>", encoding="utf-8")
        dest = pipeline.extract_document_text(src, tmp_path / "prepared")
        assert dest.suffix == ".txt"
        assert dest.read_text(encoding="utf-8") == "web words"

    def test_unsupported_suffix(self, tmp_path: Path) -> None:
        src = tmp_path / "movie.docx"
        src.write_text("x", encoding="utf-8")
        with pytest.raises(PipelineError, match="Unsupported document type"):
            pipeline.extract_document_text(src, tmp_path / "prepared")

    def test_empty_extraction_rejected(self, tmp_path: Path) -> None:
        src = tmp_path / "empty.html"
        src.write_text("<script>only code</script>", encoding="utf-8")
        with pytest.raises(PipelineError, match="No text could be extracted"):
            pipeline.extract_document_text(src, tmp_path / "prepared")

    def test_pdf_invalid_file(self, tmp_path: Path) -> None:
        src = tmp_path / "broken.pdf"
        src.write_bytes(b"not a pdf")
        with pytest.raises(PipelineError, match="Could not read PDF"):
            pipeline.pdf_to_text(src)

    def test_pdf_blank_page(self, tmp_path: Path) -> None:
        from pypdf import PdfWriter

        src = tmp_path / "blank.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        with src.open("wb") as sink:
            writer.write(sink)
        assert pipeline.pdf_to_text(src).strip() == ""


class TestSlugify:
    def test_basic(self) -> None:
        assert pipeline.slugify("Dune: Part Two (2024)") == "dune-part-two-2024"

    def test_empty_fallback(self) -> None:
        assert pipeline.slugify("???") == "wordlists"


class TestWordsmanRoot:
    def test_explicit_root(self, settings: Settings, fake_wordsman_root: Path) -> None:
        assert pipeline.resolve_wordsman_root(settings) == fake_wordsman_root

    def test_missing_root(self, tmp_path: Path) -> None:
        bad = Settings(wordsman_root=tmp_path / "nowhere")
        try:
            root = pipeline.resolve_wordsman_root(bad)
        except PipelineError:
            return  # standalone checkout: correctly rejected
        # submodule layout: the real parent wordsman checkout is a valid fallback
        assert (root / "main.py").is_file()


class TestFlows:
    async def test_movie_flow(self, settings: Settings) -> None:
        zip_path = await pipeline.movie_to_wordlists("Dune", 2021, settings)
        assert zip_path.name == "dune-2021-wordlists.zip"
        names = zipfile.ZipFile(zip_path).namelist()
        assert "subtitle-dictionary.md" in names
        assert "anki.out" in names
        assert "sparsed-yaml.out" not in names

    async def test_movie_not_found(self, settings: Settings) -> None:
        with pytest.raises(SubtitlesNotFoundError):
            await pipeline.movie_to_wordlists("Nonexistent", None, settings)

    async def test_srt_providers_forwarded(self, settings: Settings, tmp_path: Path) -> None:
        settings = settings.model_copy(update={"srt_providers": ["yify", "podnapisi"]})
        out = tmp_path / "srt"
        await pipeline.fetch_srt("Dune", 2021, settings, out)
        assert (out / ".providers").read_text() == "yify,podnapisi"

    async def test_document_flow(self, settings: Settings, tmp_path: Path) -> None:
        doc = tmp_path / "article.html"
        doc.write_text("<p>some vocabulary here</p>", encoding="utf-8")
        zip_path = await pipeline.document_to_wordlists(doc, settings, title="article")
        assert zipfile.ZipFile(zip_path).namelist()

    async def test_pipeline_failure_surfaces(self, settings: Settings, tmp_path: Path) -> None:
        doc = tmp_path / "bad.txt"
        doc.write_text("EXPLODE", encoding="utf-8")
        with pytest.raises(PipelineError, match="Wordlist generation failed"):
            await pipeline.document_to_wordlists(doc, settings, title="bad")


class TestZipDir:
    def test_roundtrip(self, tmp_path: Path) -> None:
        src = tmp_path / "out"
        (src / "nested").mkdir(parents=True)
        (src / "a.txt").write_text("a", encoding="utf-8")
        (src / "nested" / "b.txt").write_text("b", encoding="utf-8")
        zip_path = pipeline.zip_dir(src, tmp_path / "z" / "all.zip")
        assert sorted(zipfile.ZipFile(zip_path).namelist()) == ["a.txt", "nested/b.txt"]
