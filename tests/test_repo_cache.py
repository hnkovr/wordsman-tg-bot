"""Repo-cache reuse: data/in subtitles, data/out wordlists, and the LFS/degrade paths."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from tg_bot import pipeline
from tg_bot.config import Settings

LFS_POINTER = b"version https://git-lfs.github.com/spec/v1\noid sha256:deadbeef\nsize 123\n"
SRT_BODY = "1\n00:00:01,000 --> 00:00:02,000\nCached magnificent words\n"


def _put_srt(settings: Settings, slug: str, name: str = "cached.srt", body: str = SRT_BODY) -> Path:
    dest = Path(settings.repo_data_dir) / "in" / slug
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / name
    path.write_text(body, encoding="utf-8")
    return path


class TestResolveRepoDataDir:
    def test_disabled_returns_none(self, cache_settings: Settings) -> None:
        off = cache_settings.model_copy(update={"use_repo_cache": False})
        assert pipeline.resolve_repo_data_dir(off) is None

    def test_enabled_returns_dir(self, cache_settings: Settings) -> None:
        assert pipeline.resolve_repo_data_dir(cache_settings) == Path(cache_settings.repo_data_dir)

    def test_absent_dir_degrades(self, cache_settings: Settings, tmp_path: Path) -> None:
        gone = cache_settings.model_copy(update={"repo_data_dir": tmp_path / "nope"})
        assert pipeline.resolve_repo_data_dir(gone) is None

    def test_autodetects_from_wordsman_root(self, cache_settings: Settings) -> None:
        """repo_data_dir=None → <wordsman_root>/data when that exists."""
        root = Path(cache_settings.wordsman_root)
        (root / "data" / "in").mkdir(parents=True)
        auto = cache_settings.model_copy(update={"repo_data_dir": None})
        assert pipeline.resolve_repo_data_dir(auto) == root / "data"


class TestCachedSrt:
    def test_hit(self, cache_settings: Settings) -> None:
        made = _put_srt(cache_settings, "odyssey-2026")
        assert pipeline.cached_srt(cache_settings, "odyssey-2026") == made

    def test_miss_when_no_dir(self, cache_settings: Settings) -> None:
        assert pipeline.cached_srt(cache_settings, "unknown-film") is None

    def test_skips_lfs_pointer(self, cache_settings: Settings) -> None:
        dest = Path(cache_settings.repo_data_dir) / "in" / "pointer-film"
        dest.mkdir(parents=True)
        (dest / "ptr.srt").write_bytes(LFS_POINTER)
        assert pipeline.cached_srt(cache_settings, "pointer-film") is None

    def test_skips_empty_file(self, cache_settings: Settings) -> None:
        _put_srt(cache_settings, "empty-film", body="")
        assert pipeline.cached_srt(cache_settings, "empty-film") is None

    def test_disabled_never_hits(self, cache_settings: Settings) -> None:
        _put_srt(cache_settings, "odyssey-2026")
        off = cache_settings.model_copy(update={"use_repo_cache": False})
        assert pipeline.cached_srt(off, "odyssey-2026") is None


class TestCachedWordlists:
    def test_hit(self, cache_settings: Settings) -> None:
        out = Path(cache_settings.repo_data_dir) / "out" / "odyssey-2026"
        out.mkdir(parents=True)
        (out / "anki.tsv").write_text("a\tb", encoding="utf-8")
        assert pipeline.cached_wordlists(cache_settings, "odyssey-2026") == out

    def test_miss_when_empty_dir(self, cache_settings: Settings) -> None:
        (Path(cache_settings.repo_data_dir) / "out" / "bare").mkdir(parents=True)
        assert pipeline.cached_wordlists(cache_settings, "bare") is None

    def test_miss_when_only_lfs_pointers(self, cache_settings: Settings) -> None:
        out = Path(cache_settings.repo_data_dir) / "out" / "ptr"
        out.mkdir(parents=True)
        (out / "anki.tsv").write_bytes(LFS_POINTER)
        assert pipeline.cached_wordlists(cache_settings, "ptr") is None


class TestUsesDefaultGeneration:
    def test_same_is_default(self, cache_settings: Settings) -> None:
        assert pipeline.uses_default_generation(cache_settings, cache_settings)

    def test_differing_top_is_not(self, cache_settings: Settings) -> None:
        custom = cache_settings.model_copy(update={"top": 999})
        assert not pipeline.uses_default_generation(custom, cache_settings)

    def test_differing_formats_is_not(self, cache_settings: Settings) -> None:
        custom = cache_settings.model_copy(update={"formats_exclude": ["anki"]})
        assert not pipeline.uses_default_generation(custom, cache_settings)


class TestMovieFlowWithCache:
    async def test_cached_srt_skips_network(self, cache_settings: Settings) -> None:
        """A cached subtitle must be used even for a title the fetcher would fail on."""
        _put_srt(cache_settings, "nonexistent")  # fake fetch_srt.sh exits 1 for this title
        zip_path = await pipeline.movie_to_wordlists("Nonexistent", None, cache_settings)
        assert zipfile.ZipFile(zip_path).namelist()  # produced despite an unfetchable title

    async def test_prebuilt_wordlists_served_directly(self, cache_settings: Settings) -> None:
        out = Path(cache_settings.repo_data_dir) / "out" / "nonexistent"
        out.mkdir(parents=True)
        (out / "anki.tsv").write_text("prebuilt", encoding="utf-8")
        zip_path = await pipeline.movie_to_wordlists("Nonexistent", None, cache_settings)
        names = zipfile.ZipFile(zip_path).namelist()
        assert names == ["anki.tsv"]  # served verbatim, nothing regenerated

    async def test_custom_prefs_bypass_prebuilt(self, cache_settings: Settings) -> None:
        """A user with non-default prefs must get a regenerated list, not the prebuilt one."""
        out = Path(cache_settings.repo_data_dir) / "out" / "dune-2021"
        out.mkdir(parents=True)
        (out / "anki.tsv").write_text("prebuilt", encoding="utf-8")
        _put_srt(cache_settings, "dune-2021")
        custom = cache_settings.model_copy(update={"top": 42})
        zip_path = await pipeline.movie_to_wordlists("Dune", 2021, custom, defaults=cache_settings)
        names = zipfile.ZipFile(zip_path).namelist()
        assert "subtitle-dictionary.md" in names  # regenerated by the fake subtitle-dict

    async def test_falls_back_to_live_fetch_on_miss(self, cache_settings: Settings) -> None:
        zip_path = await pipeline.movie_to_wordlists("Dune", 2021, cache_settings)
        assert zipfile.ZipFile(zip_path).namelist()

    async def test_disabled_cache_uses_network(self, cache_settings: Settings) -> None:
        _put_srt(cache_settings, "nonexistent")
        off = cache_settings.model_copy(update={"use_repo_cache": False})
        with pytest.raises(pipeline.SubtitlesNotFoundError):
            await pipeline.movie_to_wordlists("Nonexistent", None, off)
