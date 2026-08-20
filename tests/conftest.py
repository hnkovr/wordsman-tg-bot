"""Shared fixtures: a fake wordsman checkout so no test touches network or real pipeline."""

from __future__ import annotations

import os
import stat
import textwrap
from pathlib import Path

import pytest

from tg_bot.config import Settings

FAKE_FETCH = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    set -euo pipefail
    movie="" year="" out="" providers=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --movie) movie="$2"; shift 2 ;;
        --year) year="$2"; shift 2 ;;
        --out) out="$2"; shift 2 ;;
        --providers) providers="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    if [ "$movie" = "Nonexistent" ]; then
      echo "no candidates" >&2
      exit 1
    fi
    mkdir -p "$out"
    printf '%s' "$providers" > "$out/.providers"
    printf '1\\n00:00:01,000 --> 00:00:02,000\\nHello magnificent world\\n' > "$out/movie.srt"
    echo "fetched $movie ${year:-}"
    echo "$out/movie.srt"
    """
)

FAKE_MAIN = textwrap.dedent(
    """\
    import argparse
    import pathlib
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("cmd")
    parser.add_argument("input")
    parser.add_argument("out")
    parser.add_argument("--formats", default="")
    args, _rest = parser.parse_known_args()

    text = pathlib.Path(args.input).read_text(encoding="utf-8")
    if "EXPLODE" in text:
        print("boom", file=sys.stderr)
        raise SystemExit(3)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for fmt in args.formats.split(","):
        (out / f"{fmt}.out").write_text(fmt, encoding="utf-8")
    (out / "subtitle-dictionary.md").write_text("# dict", encoding="utf-8")
    """
)


FAKE_SEARCH_MAIN = textwrap.dedent(
    """\
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("cmd")
    parser.add_argument("path")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args, _rest = parser.parse_known_args()

    if args.cmd == "search-subs":
        hits = [{"path": "dune-2021/Dune.ru.srt", "kind": "file", "lang": "ru",
                 "confidence": 0.97, "reasons": ["content", "filename"],
                 "stream_index": None, "codec": None, "title": None}]
    else:
        hits = [{"path": "Dune.2021.mkv", "kind": "embedded", "lang": "ru",
                 "confidence": 1.0, "reasons": ["stream_tag"], "stream_index": 2,
                 "codec": "ac3", "channels": 6, "title": None, "default": True}]
    print(json.dumps(hits))
    """
)


@pytest.fixture
def fake_search_root(tmp_path: Path) -> Path:
    """A checkout shaped like the wordsman.search module home (main.py + package dir)."""
    root = tmp_path / "wordsman-search"
    (root / "wordsman" / "search").mkdir(parents=True)
    (root / "wordsman" / "search" / "__init__.py").write_text("", encoding="utf-8")
    (root / "main.py").write_text(FAKE_SEARCH_MAIN, encoding="utf-8")
    return root


@pytest.fixture
def fake_wordsman_root(tmp_path: Path) -> Path:
    root = tmp_path / "wordsman"
    (root / "scripts").mkdir(parents=True)
    (root / "conf").mkdir()
    (root / "conf" / "settings.yml").write_text("{}\n", encoding="utf-8")
    (root / "main.py").write_text(FAKE_MAIN, encoding="utf-8")
    fetch = root / "scripts" / "fetch_srt.sh"
    fetch.write_text(FAKE_FETCH, encoding="utf-8")
    fetch.chmod(fetch.stat().st_mode | stat.S_IEXEC)
    return root


@pytest.fixture(autouse=True)
def isolate_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `Settings()` from reading the developer's config/.env (wordsman#43).

    Otherwise every "unconfigured" assertion depends on how this machine happens to be
    deployed — and, since webhook mode is enabled by configuration alone, a test could
    point the REAL bot at a test URL. Explicit env vars still win; only dotenv is cut.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", ())


@pytest.fixture
def settings(fake_wordsman_root: Path, tmp_path: Path) -> Settings:
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    return Settings(
        # No token and no public URL: no test may reach Telegram or claim its webhook.
        telegram_bot_token="",
        public_url="",
        webhook_secret="",
        wordsman_root=fake_wordsman_root,
        work_dir=tmp_path / "work",
        db_path=tmp_path / "prefs.sqlite3",
        # Default the repo cache OFF so existing tests exercise the live-fetch path;
        # cache tests opt in explicitly via the `cache_settings` fixture.
        use_repo_cache=False,
        except_list=None,
        fetch_timeout=30.0,
        dict_timeout=30.0,
    )


@pytest.fixture
def ru_settings(settings: Settings, fake_search_root: Path, tmp_path: Path) -> Settings:
    """Settings with the RU local-scan leg wired to the fake search checkout."""
    scan_dir = tmp_path / "media"
    scan_dir.mkdir(exist_ok=True)
    return settings.model_copy(
        update={
            "search_wordsman_root": fake_search_root,
            "ru_scan_dirs": [str(scan_dir)],
            "ru_search_timeout": 30.0,
        }
    )


@pytest.fixture
def cache_settings(settings: Settings, tmp_path: Path) -> Settings:
    """Settings with the repo cache enabled against an empty fake data/ dir."""
    data_dir = tmp_path / "repo-data"
    (data_dir / "in").mkdir(parents=True)
    (data_dir / "out").mkdir(parents=True)
    return settings.model_copy(update={"use_repo_cache": True, "repo_data_dir": data_dir})


@pytest.fixture
def store(settings: Settings):
    """A PrefStore on the isolated per-test DB path."""
    from tg_bot.store import PrefStore, resolve_db_path

    return PrefStore(resolve_db_path(settings))
