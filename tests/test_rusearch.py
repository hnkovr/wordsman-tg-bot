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

    async def test_russian_audio_omits_lang_flag(
        self, ru_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A wordsman checkout predating --lang must still serve the default RU search
        run, captured = _fake_run([])
        monkeypatch.setattr(rusearch, "_run", run)
        await rusearch.local_scan(ru_settings, "audio", "ru")
        assert "--lang" not in captured["cmd"]

    @pytest.mark.parametrize("lang", ["en", "original"])
    async def test_non_russian_audio_passes_lang_flag(
        self, ru_settings: Settings, monkeypatch: pytest.MonkeyPatch, lang: str
    ) -> None:
        run, captured = _fake_run([])
        monkeypatch.setattr(rusearch, "_run", run)
        await rusearch.local_scan(ru_settings, "audio", lang)
        cmd = captured["cmd"]
        assert cmd[cmd.index("--lang") + 1] == lang

    async def test_subs_never_get_a_lang_flag(
        self, ru_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run, captured = _fake_run([])
        monkeypatch.setattr(rusearch, "_run", run)
        await rusearch.local_scan(ru_settings, "subs", "en")
        assert "--lang" not in captured["cmd"]


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
    async def test_happy_path(self, ru_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
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

    def test_auto_derived_from_wordsman_root(self, ru_settings: Settings, tmp_path: Path) -> None:
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


class TestDownloadOnlineSub:
    async def test_fetches_the_chosen_candidate(
        self, ru_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = _with_subproducts(ru_settings)
        saved = tmp_path / "dl" / "Dune.ru.srt"
        saved.parent.mkdir(parents=True)
        saved.write_text("1\n", encoding="utf-8")
        run, captured = _fake_run({"path": str(saved), "file_name": saved.name, "bytes": 2})
        monkeypatch.setattr(rusearch, "_run", run)

        got = await rusearch.download_online_sub(
            cfg, "subtitlecat", "subs/42/dune", tmp_path / "dl", "Dune.ru.srt"
        )

        assert got == saved
        cmd = captured["cmd"]
        assert cmd[:5] == ["uv", "run", "srt-search", "fetch", "subtitlecat"]
        assert "subs/42/dune" in cmd and "--json" in cmd
        # The download must honour the same language as the search that listed it.
        assert captured["env"] == {"SRT_SEARCH_LANGUAGE": "ru"}

    async def test_provider_error_surfaces_its_own_words(
        self, ru_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        run, _ = _fake_run("", code=1, stderr="Error: subtitlecat has no ru track for subs/42")
        monkeypatch.setattr(rusearch, "_run", run)
        with pytest.raises(PipelineError, match="no ru track"):
            await rusearch.download_online_sub(
                _with_subproducts(ru_settings), "subtitlecat", "subs/42", tmp_path
            )

    async def test_missing_subproduct_degrades(self, ru_settings: Settings, tmp_path: Path) -> None:
        with pytest.raises(PipelineError, match="srt-search"):
            await rusearch.download_online_sub(ru_settings, "subtitlecat", "x", tmp_path)

    async def test_vanished_file_is_reported_not_returned(
        self, ru_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        run, _ = _fake_run({"path": str(tmp_path / "gone.srt"), "file_name": "gone.srt"})
        monkeypatch.setattr(rusearch, "_run", run)
        with pytest.raises(PipelineError, match="которого нет"):
            await rusearch.download_online_sub(
                _with_subproducts(ru_settings), "subtitlecat", "x", tmp_path
            )


class TestResolveLocalPath:
    def test_absolute_path_used_as_is(self, ru_settings: Settings, tmp_path: Path) -> None:
        srt = tmp_path / "media" / "Dune.ru.srt"
        srt.write_text("1", encoding="utf-8")
        assert rusearch.resolve_local_path(ru_settings, str(srt)) == srt

    def test_relative_path_resolved_against_scan_dirs(
        self, ru_settings: Settings, tmp_path: Path
    ) -> None:
        srt = tmp_path / "media" / "Dune.ru.srt"
        srt.write_text("1", encoding="utf-8")
        assert rusearch.resolve_local_path(ru_settings, "Dune.ru.srt") == srt

    def test_missing_file_is_none(self, ru_settings: Settings) -> None:
        assert rusearch.resolve_local_path(ru_settings, "/nope/absent.srt") is None


class TestCollectPicks:
    def test_online_candidates_carry_what_download_needs(self) -> None:
        picks = rusearch.collect_picks(
            mode="subs", online_subs_result=LegResult(items=SUBS_RESULT["candidates"])
        )
        assert [p.kind for p in picks] == ["online_sub"]
        assert picks[0].provider == "subtitlecat" and picks[0].candidate_id == "x"
        assert picks[0].file_name == "dune.ru.srt"

    def test_candidate_without_id_is_skipped(self) -> None:
        """A button that cannot be downloaded by is worse than no button."""
        broken = [{"provider": "subtitlecat", "title": "Dune"}]
        assert rusearch.collect_picks(mode="subs", online_subs_result=LegResult(items=broken)) == []

    def test_local_hits_become_picks_before_online_ones(self) -> None:
        picks = rusearch.collect_picks(
            mode="both",
            local_subs=[{"path": "/m/Dune.ru.srt", "kind": "file", "confidence": 0.97}],
            local_audio=[{"path": "/m/Dune.mkv", "kind": "embedded", "stream_index": 2}],
            online_subs_result=LegResult(items=SUBS_RESULT["candidates"]),
        )
        assert [p.kind for p in picks] == ["local", "local", "online_sub"]
        assert picks[0].path == "/m/Dune.ru.srt"
        assert "Dune.ru.srt" in picks[0].label and "0.97" in picks[0].label
        assert "#2" in picks[1].label

    def test_audio_only_mode_drops_subtitle_picks(self) -> None:
        picks = rusearch.collect_picks(
            mode="audio",
            local_subs=[{"path": "/m/Dune.ru.srt", "kind": "file"}],
            online_subs_result=LegResult(items=SUBS_RESULT["candidates"]),
        )
        assert picks == []

    def test_limit_caps_each_group(self) -> None:
        many = [dict(SUBS_RESULT["candidates"][0], candidate_id=f"c{i}") for i in range(10)]
        picks = rusearch.collect_picks(
            mode="subs", online_subs_result=LegResult(items=many), limit=3
        )
        assert len(picks) == 3


class TestRenderResults:
    def _sources(self) -> list[SourceLink]:
        return [
            SourceLink(
                name="rutracker",
                url="https://rutracker.org",
                access="torrent",
                search_url="https://rutracker.org/forum/tracker.php?nm={query}",
            )
        ]

    def _picks(self) -> list:
        return rusearch.collect_picks(
            mode="subs", online_subs_result=LegResult(items=SUBS_RESULT["candidates"])
        )

    def _flat(self, rows: list[list[rusearch.Button]]) -> list[rusearch.Button]:
        return [button for row in rows for button in row]

    def test_every_pick_becomes_a_button_in_order(self) -> None:
        picks = self._picks()
        _, rows = rusearch.render_results("Dune", 2021, mode="subs", session_id=7, picks=picks)
        callbacks = [b for b in self._flat(rows) if b.data]
        assert callbacks[0].data == "d:7:0"
        assert callbacks[0].label == picks[0].label

    def test_summary_counts_instead_of_listing(self) -> None:
        text, _ = rusearch.render_results(
            "Dune",
            2021,
            mode="subs",
            picks=self._picks(),
            online_subs_result=LegResult(items=SUBS_RESULT["candidates"]),
        )
        assert "1 вариант —" in text  # singular, not "1 вариантов"
        assert "скачаю и пришлю .srt" in text

    def test_russian_plural_agreement(self) -> None:
        many = [dict(SUBS_RESULT["candidates"][0], candidate_id=f"c{i}") for i in range(5)]
        picks = rusearch.collect_picks(
            mode="subs", online_subs_result=LegResult(items=many), limit=5
        )
        text, _ = rusearch.render_results("Dune", None, mode="subs", picks=picks)
        assert "5 вариантов" in text

    def test_download_all_button_only_when_several(self) -> None:
        one = rusearch.render_results("Dune", None, mode="subs", picks=self._picks(), session_id=3)
        assert not [b for b in self._flat(one[1]) if b.data == "da:3"]
        many = [dict(SUBS_RESULT["candidates"][0], candidate_id=f"c{i}") for i in range(3)]
        picks = rusearch.collect_picks(mode="subs", online_subs_result=LegResult(items=many))
        _, rows = rusearch.render_results("Dune", None, mode="subs", picks=picks, session_id=3)
        assert [b.label for b in self._flat(rows) if b.data == "da:3"] == ["⬇️ Скачать все (3)"]

    def test_sources_become_link_buttons_with_the_query(self) -> None:
        text, rows = rusearch.render_results(
            "Dune", 2021, mode="subs", subs_sources=self._sources()
        )
        links = [b for b in self._flat(rows) if b.url]
        assert links[0].url == "https://rutracker.org/forum/tracker.php?nm=Dune+2021"
        assert "Торренты: только ссылки" in text

    def test_audio_tracks_are_link_buttons_never_downloads(self) -> None:
        """A track is a whole media file — past Telegram's upload ceiling by far."""
        _, rows = rusearch.render_results(
            "Dune",
            None,
            mode="audio",
            online_audio_result=LegResult(items=AUDIO_RESULT["tracks"]),
        )
        buttons = self._flat(rows)
        assert all(b.url for b in buttons)
        assert buttons[0].url == "https://archive.org/x.mp3"

    def test_leg_reason_replaces_the_count(self) -> None:
        text, _ = rusearch.render_results(
            "Dune",
            None,
            mode="audio",
            online_audio_result=LegResult(reason="audio-search недоступен"),
        )
        assert "⚠️ audio-search недоступен" in text

    def test_subs_mode_has_no_audio_section(self) -> None:
        text, _ = rusearch.render_results("Dune", None, mode="subs")
        assert "🌐" in text and "🔊" not in text

    def test_audio_mode_has_no_subs_section(self) -> None:
        text, _ = rusearch.render_results("Dune", None, mode="audio")
        assert "🔊" in text and "🌐" not in text

    def test_english_audio_report_labels_the_language(self) -> None:
        text, _ = rusearch.render_results("Dune", 2021, mode="audio", lang="en")
        assert "поиск EN" in text and "Аудио-дорожки EN" in text

    def test_original_audio_report_says_search_is_unfiltered(self) -> None:
        text, _ = rusearch.render_results("Dune", None, mode="audio", lang="original")
        assert "поиск оригинал" in text and "без языкового фильтра" in text

    def test_titles_are_html_escaped(self) -> None:
        text, _ = rusearch.render_results("<b>Dune</b>", None, mode="subs")
        assert "&lt;b&gt;Dune&lt;/b&gt;" in text

    def test_summary_never_exceeds_telegram_limit(self) -> None:
        hits = [{"path": "x" * 300 + str(i), "kind": "file", "confidence": 0.9} for i in range(40)]
        picks = rusearch.collect_picks(mode="both", local_subs=hits, local_audio=hits, limit=40)
        text, _ = rusearch.render_results("Dune", None, mode="both", picks=picks, limit=40)
        assert len(text) <= 4000

    def test_button_labels_stay_short(self) -> None:
        hits = [{"path": "y" * 300 + ".srt", "kind": "file", "confidence": 0.9}]
        picks = rusearch.collect_picks(mode="subs", local_subs=hits)
        assert len(picks[0].label) <= rusearch._LABEL_LIMIT
