"""Subtitle & audio search: local scan + online legs + links-only sources.

Subtitles are Russian-only; audio can be searched in Russian, English, or as the
release's original (untranslated) track — see ``AUDIO_LANGS``.

Three independent legs, each degrading to an explanation instead of failing:

- local: the stdlib ``wordsman.search`` module of a second wordsman checkout
  (``search_wordsman_root``) scanning already-downloaded files on disk;
- online subs: srt-search with ``SRT_SEARCH_LANGUAGE=ru`` (subtitlecat);
- online audio: audio-search ``find --langs <lang> --json`` (feat-branch subproduct).

Sources: the ``dual_subtitle_sources`` / ``audio_sources`` catalogs rendered as
links. Torrent entries are links-only by policy — never scraped, never downloaded.

Like menu.py this module is aiogram-free: everything is unit-testable without a bot.
"""

from __future__ import annotations

import html
import json
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from tg_bot.config import Settings
from tg_bot.logger import log
from tg_bot.pipeline import PipelineError, _run, resolve_wordsman_root, stderr_reason

Kind = Literal["subs", "audio"]
#: Audio search languages. "original" is a role (the untranslated track), not a language.
AudioLang = Literal["ru", "en", "original"]
AUDIO_LANGS: tuple[str, ...] = ("ru", "en", "original")

#: lang -> (report title suffix, audio section header, message label)
LANG_LABELS: dict[str, tuple[str, str, str]] = {
    "ru": ("RU", "🔊 <b>Аудио-дорожки (онлайн)</b>", "русские"),
    "en": ("EN", "🔊 <b>Аудио-дорожки EN (онлайн)</b>", "английские"),
    "original": (
        "оригинал",
        "🔊 <b>Аудио-дорожки (онлайн, без языкового фильтра)</b>",
        "оригинальные",
    ),
}

#: Telegram rejects messages over 4096 chars; keep margin (mirrors bot._TG_LIMIT).
_TG_LIMIT = 4000

_ACCESS_LABEL = {"torrent": "торрент", "browser": "браузер", "keyless": "открытый"}


@dataclass
class SourceLink:
    """One catalog entry rendered as a manual-search link in the report."""

    name: str
    url: str
    access: str = ""
    free: bool = True
    notes: str = ""
    search_url: str | None = None


@dataclass
class LegResult:
    """Items one leg produced, or the reason it produced none."""

    items: list[dict] = field(default_factory=list)
    reason: str | None = None


def resolve_search_root(settings: Settings) -> Path:
    """Locate the checkout carrying `wordsman.search` (the local-scan engine)."""
    if not settings.search_wordsman_root:
        raise PipelineError(
            "local scan disabled: set TG_BOT_SEARCH_WORDSMAN_ROOT to a wordsman "
            "checkout containing main.py and the wordsman/search package"
        )
    root = Path(settings.search_wordsman_root).expanduser()
    if (root / "main.py").is_file() and (root / "wordsman" / "search").is_dir():
        return root
    raise PipelineError(
        f"TG_BOT_SEARCH_WORDSMAN_ROOT={root} lacks main.py or the wordsman/search package"
    )


def scan_dirs(settings: Settings) -> list[Path]:
    """Existing directories to scan for on-disk RU media; empty list disables the leg."""
    if settings.ru_scan_dirs:
        dirs = [Path(raw).expanduser() for raw in settings.ru_scan_dirs]
    else:
        try:
            dirs = [resolve_wordsman_root(settings) / "data"]
        except PipelineError:
            dirs = []
    return [directory for directory in dirs if directory.is_dir()]


async def local_scan(settings: Settings, kind: Kind, lang: str = "ru") -> list[dict]:
    """On-disk hits via `main.py search-subs|search-audio --json`; best-effort.

    `--lang` is passed only for non-Russian audio: a wordsman checkout predating the
    flag still serves the default RU search instead of failing on an unknown option.
    """
    try:
        root = resolve_search_root(settings)
    except PipelineError as exc:
        log.info("local scan skipped: {}", exc)
        return []
    subcommand = "search-subs" if kind == "subs" else "search-audio"
    extra = ["--lang", lang] if kind == "audio" and lang != "ru" else []
    hits: list[dict] = []
    for directory in scan_dirs(settings):
        cmd = [sys.executable, str(root / "main.py"), subcommand, str(directory), "--json", *extra]
        try:
            code, stdout, stderr = await _run(cmd, timeout=settings.ru_search_timeout, cwd=root)
        except PipelineError as exc:  # timeout — scan the remaining dirs anyway
            log.warning("local scan timed out for {}: {}", directory, exc)
            continue
        if code != 0:
            log.warning("local scan failed ({}): {}", code, stderr_reason(stderr, code))
            continue
        try:
            hits.extend(json.loads(stdout))
        except ValueError:
            log.warning("local scan returned non-JSON for {}", directory)
    return hits


def _failures_reason(failures: list[dict]) -> str | None:
    if not failures:
        return None
    parts = [f"{item.get('provider', '?')}: {item.get('error', '?')}" for item in failures]
    return "; ".join(parts)[:200]


async def online_subs(title: str, year: int | None, settings: Settings) -> LegResult:
    """RU subtitle candidates via srt-search (SRT_SEARCH_LANGUAGE=ru)."""
    try:
        root = resolve_wordsman_root(settings)
    except PipelineError as exc:
        return LegResult(reason=str(exc))
    subproduct = root / "subproducts" / "srt-search"
    if not (subproduct / "pyproject.toml").is_file():
        return LegResult(reason="srt-search недоступен в этом деплое")
    providers = ",".join(p.strip() for p in settings.ru_subs_providers if p.strip())
    cmd = ["uv", "run", "srt-search", "find", title, "--limit", str(settings.ru_limit)]
    if providers:
        cmd += ["--providers", providers]
    if year:
        cmd += ["--year", str(year)]
    try:
        code, stdout, stderr = await _run(
            cmd,
            timeout=settings.ru_search_timeout,
            cwd=subproduct,
            env={"SRT_SEARCH_LANGUAGE": "ru"},
        )
    except PipelineError as exc:
        return LegResult(reason=str(exc))
    result = _parse_json_object(stdout)
    if result is not None:
        candidates = result.get("candidates") or []
        reason = None if candidates else _failures_reason(result.get("failures") or [])
        return LegResult(items=candidates, reason=reason)
    if code != 0:
        return LegResult(reason=stderr_reason(stderr, code))
    return LegResult(reason="srt-search вернул не-JSON ответ")


async def online_audio(
    title: str, year: int | None, settings: Settings, lang: str = "ru"
) -> LegResult:
    """Audio-track candidates via the audio-search subproduct (`find --json`).

    `original` has no equivalent in audio-search's language filter, so that search runs
    unfiltered and the report section says so.
    """
    try:
        root = resolve_wordsman_root(settings)
    except PipelineError as exc:
        return LegResult(reason=str(exc))
    subproduct = root / "subproducts" / "audio-search"
    if not (subproduct / "pyproject.toml").is_file():
        return LegResult(reason="audio-search недоступен в этом деплое")
    cmd = ["uv", "run", "audio-search", "find", title]
    if lang != "original":
        cmd += ["--langs", lang]
    cmd += ["--limit", str(settings.ru_limit), "--json"]
    if year:
        cmd += ["--year", str(year)]
    try:
        code, stdout, stderr = await _run(cmd, timeout=settings.ru_search_timeout, cwd=subproduct)
    except PipelineError as exc:
        return LegResult(reason=str(exc))
    result = _parse_json_object(stdout)
    if result is not None:
        tracks = result.get("tracks") or []
        reason = None if tracks else _failures_reason(result.get("failures") or [])
        return LegResult(items=tracks, reason=reason)
    if code != 0:
        return LegResult(reason=stderr_reason(stderr, code))
    return LegResult(reason="audio-search вернул не-JSON ответ")


def _parse_json_object(stdout: str) -> dict | None:
    try:
        parsed = json.loads(stdout)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


#: kind -> (Settings override field, subproduct dir, catalog key in its config.yml).
_CATALOGS: dict[Kind, tuple[str, str, str]] = {
    "subs": ("ru_subs_sources_file", "srt-search", "dual_subtitle_sources"),
    "audio": ("ru_audio_sources_file", "audio-search", "audio_sources"),
}


def load_sources(settings: Settings, kind: Kind) -> list[SourceLink]:
    """The links-only source catalog for one kind; missing/broken files → empty list."""
    override, subproduct, key = _CATALOGS[kind]
    path = getattr(settings, override)
    if path is not None:
        path = Path(path).expanduser()
    else:
        try:
            root = resolve_wordsman_root(settings)
        except PipelineError:
            return []
        path = root / "subproducts" / subproduct / "config" / "config.yml"
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("ru sources: cannot read {}: {}", path, exc)
        return []
    links: list[SourceLink] = []
    for entry in data.get(key) or []:
        if not (isinstance(entry, dict) and entry.get("name") and entry.get("url")):
            continue
        links.append(
            SourceLink(
                name=str(entry["name"]),
                url=str(entry["url"]),
                access=str(entry.get("access", "")),
                free=bool(entry.get("free", True)),
                notes=str(entry.get("notes", "")),
                search_url=entry.get("search_url"),
            )
        )
    return links


def render_link(source: SourceLink, query: str) -> str:
    """The URL a user should open for this source — search template when available."""
    if source.search_url:
        return source.search_url.format(query=urllib.parse.quote_plus(query))
    return source.url


def _fmt_local_sub(hit: dict) -> str:
    where = html.escape(str(hit.get("path", "?")))
    marker = " (embedded)" if hit.get("kind") == "embedded" else ""
    confidence = hit.get("confidence", 0)
    reasons = html.escape(",".join(hit.get("reasons") or []))
    return f"• {where}{marker} — {confidence:.2f} ({reasons})"


def _fmt_local_audio(hit: dict) -> str:
    where = html.escape(str(hit.get("path", "?")))
    parts = []
    if hit.get("stream_index") is not None:
        parts.append(f"#{hit['stream_index']}")
    if hit.get("codec"):
        parts.append(html.escape(str(hit["codec"])))
    if hit.get("channels"):
        parts.append(f"{hit['channels']}ch")
    if hit.get("default"):
        parts.append("[default]")
    detail = f" — {' '.join(parts)}" if parts else ""
    return f"• {where}{detail}"


def _fmt_online_sub(candidate: dict) -> str:
    provider = html.escape(str(candidate.get("provider", "?")))
    title = html.escape(str(candidate.get("title", "?")))
    year = candidate.get("year")
    shown = f"{title} ({year})" if year else title
    extras = []
    if candidate.get("release"):
        extras.append(html.escape(str(candidate["release"])))
    if candidate.get("downloads"):
        extras.append(f"⬇{candidate['downloads']}")
    tail = f" — {' · '.join(extras)}" if extras else ""
    return f"• [{provider}] {shown}{tail}"


def _fmt_online_audio(track: dict) -> str:
    source = html.escape(str(track.get("source", "?")))
    label = html.escape(str(track.get("title") or track.get("container") or "track"))
    extras = []
    if track.get("codec"):
        extras.append(html.escape(str(track["codec"])))
    if track.get("channels"):
        extras.append(f"{track['channels']}ch")
    tail = f" — {' '.join(extras)}" if extras else ""
    url = track.get("url")
    if url:
        return f'• [{source}] <a href="{html.escape(str(url))}">{label}</a>{tail}'
    return f"• [{source}] {label}{tail}"


def _fmt_source(source: SourceLink, query: str) -> str:
    href = html.escape(render_link(source, query), quote=True)
    label = html.escape(source.name)
    access = _ACCESS_LABEL.get(source.access, source.access)
    return f'• <a href="{href}">{label}</a> — {html.escape(access)}'


def _section(
    header: str, lines: list[str], *, reason: str | None = None, empty: str = "• ничего не найдено"
) -> list[str]:
    out = ["", header]
    if lines:
        out.extend(lines)
    elif reason:
        out.append(f"⚠️ {html.escape(reason)}")
    else:
        out.append(empty)
    return out


def format_report(
    title: str,
    year: int | None,
    *,
    mode: str = "both",
    local_subs: list[dict] | None = None,
    local_audio: list[dict] | None = None,
    online_subs_result: LegResult | None = None,
    online_audio_result: LegResult | None = None,
    subs_sources: list[SourceLink] | None = None,
    audio_sources: list[SourceLink] | None = None,
    limit: int = 5,
    lang: str = "ru",
) -> str:
    """The full search reply as Telegram HTML; sections depend on `mode` and `lang`."""
    shown = f"{title} ({year})" if year else title
    query = f"{title} {year}" if year else title
    want_subs = mode in ("both", "subs")
    want_audio = mode in ("both", "audio")
    title_label, audio_header, _ = LANG_LABELS.get(lang, LANG_LABELS["ru"])

    lines = [f"🔎 <b>{html.escape(shown)}</b> — поиск {title_label}"]

    local: list[str] = []
    if want_subs:
        local += [_fmt_local_sub(hit) for hit in (local_subs or [])[:limit]]
    if want_audio:
        local += [_fmt_local_audio(hit) for hit in (local_audio or [])[:limit]]
    lines += _section("📀 <b>Локально (уже на диске)</b>", local)

    if want_subs:
        result = online_subs_result or LegResult()
        lines += _section(
            "🌐 <b>Онлайн-субтитры</b>",
            [_fmt_online_sub(c) for c in result.items[:limit]],
            reason=result.reason,
        )
    if want_audio:
        result = online_audio_result or LegResult()
        lines += _section(
            audio_header,
            [_fmt_online_audio(t) for t in result.items[:limit]],
            reason=result.reason,
        )

    sources: list[SourceLink] = []
    seen: set[str] = set()
    for source in (subs_sources or []) if want_subs else []:
        sources.append(source)
        seen.add(source.name)
    for source in (audio_sources or []) if want_audio else []:
        if source.name not in seen:
            sources.append(source)
    if sources:
        lines += _section("🔗 <b>Где искать вручную</b>", [_fmt_source(s, query) for s in sources])
        if any(s.access == "torrent" for s in sources):
            lines.append("<i>Торренты: только ссылки на поиск — скачивание вручную.</i>")

    text = "\n".join(lines)
    return text if len(text) <= _TG_LIMIT else text[: _TG_LIMIT - 1] + "…"
