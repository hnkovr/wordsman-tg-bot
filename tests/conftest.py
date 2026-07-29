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


@pytest.fixture
def settings(fake_wordsman_root: Path, tmp_path: Path) -> Settings:
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    return Settings(
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
