"""RU search legs: resolvers, local subprocess scan, online legs, sources, report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tg_bot import rusearch
from tg_bot.config import Settings
from tg_bot.pipeline import PipelineError
from tg_bot.rusearch import LegResult, SourceLink

SUBS_RESULT = {
    "movie": "Dune",
    "candidates": [
        {
            "provider": "subtitlecat",
            "candidate_id": "x",
            "title": "Dune",
            "year": 2021,
            "release": "dune-2021.ru.srt",
            "language": "ru",
            "downloads": 1523,
            "file_name": "dune.ru.srt",
        }
    ],
    "failures": [],
}
AUDIO_RESULT = {
    "movie": "Dune",
    "tracks": [
        {
            "source": "archive",
            "language": "ru",
            "container": "mp3",
            "url": "https://archive.org/x.mp3",
            "codec": "mp3",
            "channels": 2,
            "title": "Dune radio drama",
            "is_default": False,
        }
    ],
    "failures": [],
}

CATALOG_YAML = """\
dual_subtitle_sources:
  - name: rutracker
    url: https://rutracker.org
    access: torrent
    free: true
    search_url: "https://rutracker.org/forum/tracker.php?nm={query}"
    notes: separate audio tracks and subs
  - name: subtitlecat
    url: https://www.subtitlecat.com
    access: keyless
    free: true
    notes: no search_url on purpose
  - name: broken-entry
    url:
"""


def _with_subproducts(settings: Settings) -> Settings:
    root = Path(settings.wordsman_root)
    for sub in ("srt-search", "audio-search"):
        (root / "subproducts" / sub).mkdir(parents=True, exist_ok=True)
        (root / "subproducts" / sub / "pyproject.toml").write_text("", encoding="utf-8")
    return settings


def _fake_run(payload: dict | str, code: int = 0, stderr: str = ""):
    captured: dict = {}

    async def run(cmd, *, timeout, cwd, env=None):
        captured.update(cmd=cmd, timeout=timeout, cwd=cwd, env=env)
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        return code, stdout, stderr

    return run, captured


class TestResolveSearchRoot:
    def test_unset_raises_actionable_error(self, settings: Settings) -> None:
        with pytest.raises(PipelineError, match="TG_BOT_SEARCH_WORDSMAN_ROOT"):
            rusearch.resolve_search_root(settings)

    def test_root_without_search_package_rejected(self, settings: Settings) -> None:
        # The fetch_srt.sh checkout is NOT a valid search root (no wordsman/search).
        bad = settings.model_copy(update={"search_wordsman_root": settings.wordsman_root})
        with pytest.raises(PipelineError, match="wordsman/search"):
            rusearch.resolve_search_root(bad)

    def test_valid_root_accepted(self, ru_settings: Settings, fake_search_root: Path) -> None:
        assert rusearch.resolve_search_root(ru_settings) == fake_search_root

    def test_tilde_expanded(
        self,
        ru_settings: Settings,
        fake_search_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(fake_search_root.parent))
        cfg = ru_settings.model_copy(
            update={"search_wordsman_root": Path("~") / fake_search_root.name}
        )
        assert rusearch.resolve_search_root(cfg) == fake_search_root


class TestScanDirs:
    def test_explicit_dirs_filtered_to_existing(
        self, ru_settings: Settings, tmp_path: Path
    ) -> None:
        cfg = ru_settings.model_copy(
            update={"ru_scan_dirs": [str(tmp_path / "media"), str(tmp_path / "missing")]}
        )
        assert rusearch.scan_dirs(cfg) == [tmp_path / "media"]

    def test_empty_falls_back_to_wordsman_data(self, ru_settings: Settings) -> None:
        data = Path(ru_settings.wordsman_root) / "data"
        data.mkdir()
        cfg = ru_settings.model_copy(update={"ru_scan_dirs": []})
        assert rusearch.scan_dirs(cfg) == [data]


class TestLocalScan:
    async def test_subs_hits_parsed(self, ru_settings: Settings) -> None:
        hits = await rusearch.local_scan(ru_settings, "subs")
        assert hits and hits[0]["path"] == "dune-2021/Dune.ru.srt"

    async def test_audio_hits_parsed(self, ru_settings: Settings) -> None:
        hits = await rusearch.local_scan(ru_settings, "audio")
        assert hits and hits[0]["stream_index"] == 2

    async def test_unconfigured_root_returns_empty(self, settings: Settings) -> None:
        assert await rusearch.local_scan(settings, "subs") == []

    async def test_non_json_output_tolerated(
        self, ru_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run, _ = _fake_run("garbage")
        monkeypatch.setattr(rusearch, "_run", run)
        assert await rusearch.local_scan(ru_settings, "subs") == []


class TestOnlineSubs:
    async def test_happy_path_passes_ru_env_and_providers(
        self, ru_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _with_subproducts(ru_settings)
        run, captured = _fake_run(SUBS_RESULT)
        monkeypatch.setattr(rusearch, "_run", run)
        result = await rusearch.online_subs("Dune", 2021, cfg)
        assert result.reason is None
        assert result.items[0]["provider"] == "subtitlecat"
        assert captured["env"] == {"SRT_SEARCH_LANGUAGE": "ru"}
        assert "--providers" in captured["cmd"] and "subtitlecat" in captured["cmd"]
        assert "--year" in captured["cmd"]

    async def test_missing_subproduct_degrades(self, ru_settings: Settings) -> None:
        result = await rusearch.online_subs("Dune", None, ru_settings)
        assert result.items == [] and "srt-search" in result.reason

    async def test_failure_reason_from_stderr(
        self, ru_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run, _ = _fake_run("", code=1, stderr="ProviderError: upstream broke")
        monkeypatch.setattr(rusearch, "_run", run)
        result = await rusearch.online_subs("Dune", None, _with_subproducts(ru_settings))
        assert result.reason == "ProviderError: upstream broke"

    async def test_timeout_becomes_reason(
        self, ru_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def run(cmd, *, timeout, cwd, env=None):
            raise PipelineError("'uv' timed out after 30s")

        monkeypatch.setattr(rusearch, "_run", run)
        result = await rusearch.online_subs("Dune", None, _with_subproducts(ru_settings))
        assert "timed out" in result.reason

    async def test_empty_candidates_surface_provider_failures(
        self, ru_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {
            "movie": "X",
            "candidates": [],
            "failures": [{"provider": "subtitlecat", "error": "HTTP 503"}],
        }
        run, _ = _fake_run(payload)
        monkeypatch.setattr(rusearch, "_run", run)
        result = await rusearch.online_subs("X", None, _with_subproducts(ru_settings))
        assert result.items == [] and "subtitlecat: HTTP 503" in result.reason


class TestOnlineAudio:
    async def test_happy_path(
        self, ru_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run, captured = _fake_run(AUDIO_RESULT)
        monkeypatch.setattr(rusearch, "_run", run)
        result = await rusearch.online_audio("Dune", 2021, _with_subproducts(ru_settings))
        assert result.reason is None and result.items[0]["source"] == "archive"
        assert "--langs" in captured["cmd"] and "ru" in captured["cmd"]
        assert "--json" in captured["cmd"]

    async def test_missing_subproduct_degrades(self, ru_settings: Settings) -> None:
        result = await rusearch.online_audio("Dune", None, ru_settings)
        assert result.items == [] and "audio-search" in result.reason


class TestSources:
    def test_catalog_parsed_and_broken_entries_skipped(
        self, ru_settings: Settings, tmp_path: Path
    ) -> None:
        catalog = tmp_path / "catalog.yml"
        catalog.write_text(CATALOG_YAML, encoding="utf-8")
        cfg = ru_settings.model_copy(update={"ru_subs_sources_file": catalog})
        sources = rusearch.load_sources(cfg, "subs")
        assert [s.name for s in sources] == ["rutracker", "subtitlecat"]
        assert sources[0].access == "torrent"

    def test_missing_file_returns_empty(self, ru_settings: Settings, tmp_path: Path) -> None:
        cfg = ru_settings.model_copy(update={"ru_subs_sources_file": tmp_path / "nope.yml"})
        assert rusearch.load_sources(cfg, "subs") == []

    def test_auto_derived_from_wordsman_root(
        self, ru_settings: Settings, tmp_path: Path
    ) -> None:
        cfg = _with_subproducts(ru_settings)
        config_dir = Path(cfg.wordsman_root) / "subproducts" / "srt-search" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yml").write_text(CATALOG_YAML, encoding="utf-8")
        assert [s.name for s in rusearch.load_sources(cfg, "subs")] == [
            "rutracker",
            "subtitlecat",
        ]

    def test_render_link_substitutes_query(self) -> None:
        source = SourceLink(
            name="rutracker",
            url="https://rutracker.org",
            search_url="https://rutracker.org/forum/tracker.php?nm={query}",
        )
        assert (
            rusearch.render_link(source, "Dune 2021")
            == "https://rutracker.org/forum/tracker.php?nm=Dune+2021"
        )

    def test_render_link_without_template_uses_url(self) -> None:
        source = SourceLink(name="x", url="https://example.org")
        assert rusearch.render_link(source, "Dune") == "https://example.org"


class TestFormatReport:
    def _sources(self) -> list[SourceLink]:
        return [
            SourceLink(
                name="rutracker",
                url="https://rutracker.org",
                access="torrent",
                search_url="https://rutracker.org/forum/tracker.php?nm={query}",
            )
        ]

    def test_both_mode_renders_all_sections(self) -> None:
        report = rusearch.format_report(
            "Dune",
            2021,
            mode="both",
            local_subs=SUBS_RESULT["candidates"],
            online_subs_result=LegResult(items=SUBS_RESULT["candidates"]),
            online_audio_result=LegResult(reason="audio-search недоступен"),
            subs_sources=self._sources(),
        )
        for marker in ("📀", "🌐", "🔊", "🔗"):
            assert marker in report
        assert "rutracker.org/forum/tracker.php?nm=Dune+2021" in report
        assert "Торренты: только ссылки" in report
        assert "⚠️ audio-search недоступен" in report

    def test_subs_mode_has_no_audio_section(self) -> None:
        report = rusearch.format_report("Dune", None, mode="subs")
        assert "🌐" in report and "🔊" not in report

    def test_audio_mode_has_no_subs_section(self) -> None:
        report = rusearch.format_report("Dune", None, mode="audio")
        assert "🔊" in report and "🌐" not in report

    def test_titles_are_html_escaped(self) -> None:
        report = rusearch.format_report("<b>Dune</b>", None, mode="subs")
        assert "&lt;b&gt;Dune&lt;/b&gt;" in report

    def test_sections_capped_by_limit(self) -> None:
        hits = [{"path": f"file-{i}.srt", "kind": "file", "confidence": 0.9} for i in range(10)]
        report = rusearch.format_report("Dune", None, mode="subs", local_subs=hits, limit=2)
        assert report.count("file-") == 2

    def test_report_never_exceeds_telegram_limit(self) -> None:
        hits = [
            {"path": "x" * 300 + str(i), "kind": "file", "confidence": 0.9} for i in range(40)
        ]
        report = rusearch.format_report(
            "Dune", None, mode="both", local_subs=hits, local_audio=hits, limit=40
        )
        assert len(report) <= 4000
