"""On-disk artifact discovery for the send-files menu."""

from __future__ import annotations

from pathlib import Path

from tg_bot import artifacts
from tg_bot.config import Settings


def _touch(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class TestDiscovery:
    def test_subtitles_from_work_and_data(
        self, settings: Settings, fake_wordsman_root: Path
    ) -> None:
        _touch(Path(settings.work_dir) / "dune-2021" / "srt" / "Dune.srt")
        _touch(fake_wordsman_root / "data" / "in" / "matrix" / "Matrix.vtt")
        slugs = {a.slug for a in artifacts.available(settings, "subtitle")}
        assert {"dune-2021", "matrix"} <= slugs

    def test_wordlists_are_the_zip_bundles(self, settings: Settings) -> None:
        _touch(Path(settings.work_dir) / "dune-2021" / "dune-2021-wordlists.zip")
        _touch(Path(settings.work_dir) / "dune-2021" / "out" / "anki.tsv")  # not a bundle
        items = artifacts.available(settings, "wordlist")
        assert [a.slug for a in items] == ["dune-2021"]
        assert items[0].path.name == "dune-2021-wordlists.zip"

    def test_audio_from_data_tree(self, settings: Settings, fake_wordsman_root: Path) -> None:
        _touch(fake_wordsman_root / "data" / "audio" / "dune" / "Dune.en.m4a")
        items = artifacts.available(settings, "audio")
        assert items and items[0].slug == "dune" and items[0].kind == "audio"

    def test_empty_when_nothing_generated(self, settings: Settings) -> None:
        assert artifacts.available(settings, "subtitle") == []
        assert artifacts.available(settings, "wordlist") == []

    def test_capped_to_max_items(self, settings: Settings) -> None:
        for i in range(artifacts.MAX_ITEMS + 5):
            _touch(Path(settings.work_dir) / f"movie{i}" / "srt" / f"{i}.srt")
        assert len(artifacts.available(settings, "subtitle")) == artifacts.MAX_ITEMS

    def test_slug_skips_generic_dirs(self, settings: Settings) -> None:
        _touch(Path(settings.work_dir) / "42" / "blade-runner" / "srt" / "x.srt")
        slugs = {a.slug for a in artifacts.available(settings, "subtitle")}
        assert "blade-runner" in slugs  # nearest meaningful dir, not "srt"
