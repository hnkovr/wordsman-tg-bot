"""Discover already-generated artifacts on disk so the bot can re-send them.

Three kinds, each surfaced as a separate menu item:
- subtitle: `.srt`/`.vtt` fetched into the bot's work dir or the wordsman `data/` tree
- wordlist: the `*-wordlists.zip` bundles the pipeline produced
- audio:    audio tracks (from the audio-search subproduct) under the wordsman `data/` tree

Read-only: this never generates anything, it only lists and returns paths to send.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tg_bot.config import REPO_ROOT, Settings
from tg_bot.pipeline import resolve_wordsman_root

SUBTITLE_EXTS = {".srt", ".vtt"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".opus", ".flac", ".wav", ".ogg"}
#: Directory names that describe the layout, not the content — skipped when naming an item.
_GENERIC_DIRS = {"srt", "out", "src", "work", "data", "in", "downloads", "audio", "upload"}

MAX_ITEMS = 25  # keep the inline keyboard within Telegram limits
MAX_SEND_BYTES = 50 * 1024 * 1024  # Telegram bot API upload ceiling (cloud API)


@dataclass(frozen=True)
class Artifact:
    slug: str
    kind: str
    path: Path

    @property
    def size(self) -> int:
        return self.path.stat().st_size


def _work_base(settings: Settings) -> Path:
    base = Path(settings.work_dir)
    return base if base.is_absolute() else REPO_ROOT / base


def _data_dirs(settings: Settings) -> list[Path]:
    try:
        root = resolve_wordsman_root(settings)
    except Exception:
        return []
    return [d for d in (root / "data",) if d.is_dir()]


def _slug_for(path: Path) -> str:
    """Nearest meaningful ancestor directory (the movie slug), else the file stem."""
    for part in reversed(path.parts[:-1]):
        if part.lower() not in _GENERIC_DIRS:
            return part
    return path.stem


def _scan(dirs: list[Path], exts: set[str]) -> list[Path]:
    found: dict[Path, Path] = {}
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and path.suffix.lower() in exts:
                found[path.resolve()] = path
    return list(found.values())


def find_subtitles(settings: Settings) -> list[Artifact]:
    dirs = [_work_base(settings), *_data_dirs(settings)]
    return [Artifact(_slug_for(p), "subtitle", p) for p in _scan(dirs, SUBTITLE_EXTS)]


def find_wordlists(settings: Settings) -> list[Artifact]:
    base = _work_base(settings)
    zips = sorted(base.rglob("*-wordlists.zip")) if base.is_dir() else []
    return [Artifact(p.stem.replace("-wordlists", ""), "wordlist", p) for p in zips]


def find_audio(settings: Settings) -> list[Artifact]:
    return [Artifact(_slug_for(p), "audio", p) for p in _scan(_data_dirs(settings), AUDIO_EXTS)]


_FINDERS = {"subtitle": find_subtitles, "wordlist": find_wordlists, "audio": find_audio}


def available(settings: Settings, kind: str) -> list[Artifact]:
    """Available artifacts of one kind, newest first, capped for the keyboard."""
    items = _FINDERS[kind](settings)
    items.sort(key=lambda a: a.path.stat().st_mtime, reverse=True)
    return items[:MAX_ITEMS]
