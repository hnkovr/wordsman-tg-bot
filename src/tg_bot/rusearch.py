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

Results are returned as a short summary plus a keyboard (``render_results``): every
subtitle a user could want becomes a button that delivers the actual file, and only the
manual/torrent sources stay links.

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
from tg_bot.picks import Pick
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
#: Inline-button text is truncated by clients well before Telegram rejects it.
_LABEL_LIMIT = 48

#: srt-search reads its language from the environment; subtitles here are RU-only, and
#: the DOWNLOAD honours it too — so a candidate without a RU track fails instead of
#: quietly delivering the English one.
_RU_ENV = {"SRT_SEARCH_LANGUAGE": "ru"}

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


@dataclass(frozen=True)
class Button:
    """One inline-keyboard button: either a callback (`data`) or a link (`url`)."""

    label: str
    data: str | None = None
    url: str | None = None


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
            env=_RU_ENV,
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


# ── downloading one chosen candidate ──────────────────────────────────────────────


async def download_online_sub(
    settings: Settings,
    provider: str,
    candidate_id: str,
    dest_dir: Path,
    file_name: str = "",
) -> Path:
    """Download the ONE candidate a user tapped, via `srt-search fetch`.

    Distinct from the search leg: no ranking, no "best" heuristic — the choice was already
    made in the chat. Failure is loud (PipelineError with the provider's own words), since
    a listed candidate may simply have no Russian track.
    """
    root = resolve_wordsman_root(settings)
    subproduct = root / "subproducts" / "srt-search"
    if not (subproduct / "pyproject.toml").is_file():
        raise PipelineError("srt-search недоступен в этом деплое")
    dest_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "uv",
        "run",
        "srt-search",
        "fetch",
        provider,
        candidate_id,
        "--out",
        str(dest_dir),
        "--json",
    ]
    if file_name:
        cmd += ["--name", file_name]
    code, stdout, stderr = await _run(
        cmd, timeout=settings.ru_search_timeout, cwd=subproduct, env=_RU_ENV
    )
    payload = _parse_json_object(stdout)
    if code != 0 or payload is None:
        raise PipelineError(stderr_reason(stderr, code))
    saved = Path(str(payload.get("path", "")))
    if not saved.is_file():
        raise PipelineError(f"srt-search сообщил о файле {saved}, которого нет")
    return saved


def resolve_local_path(settings: Settings, raw: str) -> Path | None:
    """Turn a scan hit's path into a real file: absolute as-is, else per scan dir."""
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path if path.is_file() else None
    for directory in scan_dirs(settings):
        candidate = directory / path
        if candidate.is_file():
            return candidate
    return None


# ── labels: what a button says ────────────────────────────────────────────────────


def _clip(text: str, limit: int = _LABEL_LIMIT) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _local_sub_label(hit: dict) -> str:
    name = Path(str(hit.get("path", "?"))).name
    confidence = hit.get("confidence")
    score = f" · {confidence:.2f}" if isinstance(confidence, int | float) else ""
    where = " (в контейнере)" if hit.get("kind") == "embedded" else ""
    return _clip(f"📄 {name}{where}{score}")


def _local_audio_label(hit: dict) -> str:
    name = Path(str(hit.get("path", "?"))).name
    parts: list[str] = []
    if hit.get("stream_index") is not None:
        parts.append(f"#{hit['stream_index']}")
    if hit.get("codec"):
        parts.append(str(hit["codec"]))
    if hit.get("channels"):
        parts.append(f"{hit['channels']}ch")
    detail = f" · {' '.join(parts)}" if parts else ""
    return _clip(f"🔊 {name}{detail}")


def _online_sub_label(candidate: dict) -> str:
    name = candidate.get("release") or candidate.get("title") or candidate.get("file_name") or "?"
    year = candidate.get("year")
    provider = candidate.get("provider", "?")
    return _clip(f"📄 {name}{f' ({year})' if year else ''} · {provider}")


def _online_audio_label(track: dict) -> str:
    name = track.get("title") or track.get("container") or "track"
    parts = [str(track[key]) for key in ("codec",) if track.get(key)]
    if track.get("channels"):
        parts.append(f"{track['channels']}ch")
    detail = f" · {' '.join(parts)}" if parts else ""
    return _clip(f"🔊 {name} · {track.get('source', '?')}{detail}")


def collect_picks(
    *,
    mode: str = "both",
    local_subs: list[dict] | None = None,
    local_audio: list[dict] | None = None,
    online_subs_result: LegResult | None = None,
    limit: int = 5,
) -> list[Pick]:
    """Everything the user can tap, in the order the keyboard will show it.

    Online AUDIO is deliberately absent: those tracks are whole media files, far past
    Telegram's 50 MB upload ceiling, so they stay link buttons instead.
    """
    picks: list[Pick] = []
    if mode in ("both", "subs"):
        for hit in (local_subs or [])[:limit]:
            picks.append(
                Pick(kind="local", label=_local_sub_label(hit), path=str(hit.get("path", "")))
            )
    if mode in ("both", "audio"):
        for hit in (local_audio or [])[:limit]:
            picks.append(
                Pick(kind="local", label=_local_audio_label(hit), path=str(hit.get("path", "")))
            )
    if mode in ("both", "subs"):
        for candidate in (online_subs_result or LegResult()).items[:limit]:
            if not candidate.get("candidate_id"):
                continue  # nothing to download it by — it would be a dead button
            picks.append(
                Pick(
                    kind="online_sub",
                    label=_online_sub_label(candidate),
                    provider=str(candidate.get("provider", "")),
                    candidate_id=str(candidate["candidate_id"]),
                    file_name=str(candidate.get("file_name") or ""),
                )
            )
    return picks


# ── the reply: a short summary plus the keyboard ──────────────────────────────────


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Russian noun agreement — "1 вариант", "2 варианта", "5 вариантов"."""
    tail, hundred = count % 10, count % 100
    if tail == 1 and hundred != 11:
        return f"{count} {one}"
    if 2 <= tail <= 4 and not 12 <= hundred <= 14:
        return f"{count} {few}"
    return f"{count} {many}"


def _fmt_source(source: SourceLink, query: str) -> str:
    href = html.escape(render_link(source, query), quote=True)
    label = html.escape(source.name)
    access = _ACCESS_LABEL.get(source.access, source.access)
    return f'• <a href="{href}">{label}</a> — {html.escape(access)}'


def _section(header: str, body: list[str]) -> list[str]:
    return ["", header, *body]


def _status(count: int, noun: tuple[str, str, str], action: str, reason: str | None) -> list[str]:
    """One section's body: what was found and what to do, or why nothing was."""
    if count:
        return [f"• {_plural(count, *noun)} — {action}"]
    if reason:
        return [f"⚠️ {html.escape(reason)}"]
    return ["• ничего не найдено"]


def render_results(
    title: str,
    year: int | None,
    *,
    mode: str = "both",
    lang: str = "ru",
    session_id: int = 0,
    picks: list[Pick] | None = None,
    local_subs: list[dict] | None = None,
    local_audio: list[dict] | None = None,
    online_subs_result: LegResult | None = None,
    online_audio_result: LegResult | None = None,
    subs_sources: list[SourceLink] | None = None,
    audio_sources: list[SourceLink] | None = None,
    limit: int = 5,
) -> tuple[str, list[list[Button]]]:
    """The search reply: (HTML summary, keyboard rows).

    The summary says what each leg found; the keyboard carries the results themselves —
    `d:<session>:<index>` buttons deliver a file, link buttons open a page. Button order
    matches `picks` exactly, so the index in the callback data is the index in the store.
    """
    picks = picks or []
    shown = f"{title} ({year})" if year else title
    query = f"{title} {year}" if year else title
    want_subs = mode in ("both", "subs")
    want_audio = mode in ("both", "audio")
    title_label, audio_header, _ = LANG_LABELS.get(lang, LANG_LABELS["ru"])

    lines = [f"🔎 <b>{html.escape(shown)}</b> — поиск {title_label}"]
    rows: list[list[Button]] = [
        [Button(label=pick.label, data=f"d:{session_id}:{index}")]
        for index, pick in enumerate(picks)
    ]

    local_count = len([p for p in picks if p.kind == "local"])
    lines += _section(
        "📀 <b>Локально (уже на диске)</b>",
        _status(local_count, ("файл", "файла", "файлов"), "жмите кнопку, пришлю файлом", None),
    )

    if want_subs:
        result = online_subs_result or LegResult()
        online_count = len([p for p in picks if p.kind == "online_sub"])
        lines += _section(
            "🌐 <b>Онлайн-субтитры</b>",
            _status(
                online_count,
                ("вариант", "варианта", "вариантов"),
                "жмите кнопку, скачаю и пришлю .srt",
                result.reason,
            ),
        )
        if online_count > 1:
            rows.append([Button(label=f"⬇️ Скачать все ({online_count})", data=f"da:{session_id}")])

    if want_audio:
        result = online_audio_result or LegResult()
        tracks = result.items[:limit]
        linked = [t for t in tracks if t.get("url")]
        lines += _section(
            audio_header,
            _status(
                len(tracks),
                ("дорожка", "дорожки", "дорожек"),
                "ссылки на кнопках ниже" if linked else "без прямых ссылок",
                result.reason,
            ),
        )
        rows += [
            [Button(label=_online_audio_label(track), url=str(track["url"]))] for track in linked
        ]

    sources: list[SourceLink] = []
    seen: set[str] = set()
    for source in (subs_sources or []) if want_subs else []:
        sources.append(source)
        seen.add(source.name)
    for source in (audio_sources or []) if want_audio else []:
        if source.name not in seen:
            sources.append(source)
    if sources:
        lines += _section("🔗 <b>Где искать вручную</b>", ["• кнопки-ссылки ниже"])
        if any(s.access == "torrent" for s in sources):
            lines.append("<i>Торренты: только ссылки на поиск — скачивание вручную.</i>")
        link_buttons = [
            Button(label=_clip(f"🔗 {s.name}"), url=render_link(s, query)) for s in sources
        ]
        rows += [link_buttons[i : i + 2] for i in range(0, len(link_buttons), 2)]

    text = "\n".join(lines)
    return (text if len(text) <= _TG_LIMIT else text[: _TG_LIMIT - 1] + "…"), rows
