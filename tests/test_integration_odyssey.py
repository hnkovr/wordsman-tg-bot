"""API integration tests for "The Odyssey" (2026) against the REAL wordsman checkout.

Unlike the rest of the suite (hermetic: fake checkout, no network, no real pipeline),
these drive the parent repo's actual `scripts/fetch_srt.sh` and `main.py subtitle-dict`
and read its Git-LFS `data/` blobs. They are therefore **deselected by default** —
pyproject pins `-m "not integration"` — and opted into explicitly:

    just test-integration            # cache lane: real repo + real generation, no network
    just test-integration-network    # + the live provider fetch

Goal: prove one API call reproduces the two artifact trees an earlier CLI run left in the
repo — `data/in/odyssey-2026/**` (the fetched subtitle) and `data/out/odyssey-2026/**`
(the built wordlists). This is the exact query that used to 404 back when the bot ignored
the repo cache and shipped a podnapisi-only provider set (wordsman#22).
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tg_bot import pipeline
from tg_bot.api import create_app
from tg_bot.config import REPO_ROOT, Settings, get_settings
from tg_bot.store import get_store

#: The title as a user would type it, and the slug both data/in and data/out already use.
QUERY = "odyssey 2026"
TITLE, YEAR = "odyssey", 2026
SLUG = "odyssey-2026"
#: The full title, for the live-fetch lane — providers need more than the bare word.
LIVE_TITLE = "The Odyssey"
USER = 4242

#: Keep the regeneration lanes cheap: two formats and a short list instead of all 14.
FAST_FORMATS = ("md", "quizlet")
FAST_EXCLUDE = [f for f in pipeline.SUPPORTED_FORMATS if f not in FAST_FORMATS]
FAST_TOP = 25


def _find_wordsman_root() -> Path | None:
    """The real wordsman checkout: env override, submodule layout, then sibling clone."""
    candidates: list[Path] = []
    if env := os.environ.get("TG_BOT_WORDSMAN_ROOT"):
        candidates.append(Path(env))
    candidates.append(REPO_ROOT.parent.parent)  # <wordsman>/subproducts/tg-bot
    candidates.append(REPO_ROOT.parent / "wordsman")  # standalone sibling checkout
    for root in candidates:
        if (root / "main.py").is_file() and (root / "scripts" / "fetch_srt.sh").is_file():
            return root
    return None


WORDSMAN_ROOT = _find_wordsman_root()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        WORDSMAN_ROOT is None,
        reason="no wordsman checkout found; set TG_BOT_WORDSMAN_ROOT to one",
    ),
]


@pytest.fixture(scope="session")
def wordsman_root() -> Path:
    assert WORDSMAN_ROOT is not None  # guarded by the module-level skipif
    return WORDSMAN_ROOT


def _usable_files(directory: Path) -> list[Path]:
    """Real files under `directory`, ignoring unfetched Git-LFS pointers."""
    if not directory.is_dir():
        return []
    return [
        p
        for p in sorted(directory.rglob("*"))
        if p.is_file() and p.stat().st_size and not pipeline._is_lfs_pointer(p)
    ]


@pytest.fixture(scope="session")
def cached_in(wordsman_root: Path) -> Path:
    """`data/in/odyssey-2026` with a real subtitle in it, or skip with the reason why."""
    src = wordsman_root / "data" / "in" / SLUG
    if not [p for p in _usable_files(src) if p.suffix == ".srt"]:
        pytest.skip(f"{src} holds no usable .srt — run `git lfs pull` in {wordsman_root}")
    return src


@pytest.fixture(scope="session")
def prebuilt_out(wordsman_root: Path) -> Path:
    """`data/out/odyssey-2026` with real wordlists in it, or skip with the reason why."""
    out = wordsman_root / "data" / "out" / SLUG
    if not _usable_files(out):
        pytest.skip(f"{out} holds no usable wordlists — run `git lfs pull` in {wordsman_root}")
    return out


def _settings(wordsman_root: Path, tmp_path: Path, **overrides: object) -> Settings:
    """Real checkout, but scratch dirs and prefs DB isolated to this test."""
    fields: dict[str, object] = {
        "wordsman_root": wordsman_root,
        "work_dir": tmp_path / "work",
        "db_path": tmp_path / "prefs.sqlite3",
        "use_repo_cache": True,
        "repo_data_dir": None,  # exercise auto-detection of <wordsman_root>/data
    }
    fields.update(overrides)
    return Settings(**fields)


@pytest.fixture
def live_settings(wordsman_root: Path, tmp_path: Path) -> Settings:
    return _settings(wordsman_root, tmp_path)


@pytest.fixture
def nocache_settings(wordsman_root: Path, tmp_path: Path) -> Settings:
    """Repo cache off, so every lookup must go to the live providers."""
    return _settings(
        wordsman_root,
        tmp_path,
        use_repo_cache=False,
        formats_exclude=FAST_EXCLUDE,
        top=FAST_TOP,
    )


def _client(settings: Settings) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


@pytest.fixture
def client(live_settings: Settings) -> TestClient:
    return _client(live_settings)


def _zip_names(response: httpx.Response) -> set[str]:
    return set(zipfile.ZipFile(io.BytesIO(response.content)).namelist())


class TestQueryMapsToTheCachedArtifacts:
    """The bare user query must resolve to the slug the repo artifacts are filed under."""

    def test_bare_query_parses_to_title_and_year(self) -> None:
        assert pipeline.parse_movie_query(QUERY) == (TITLE, YEAR)

    def test_parsed_query_slugifies_to_the_data_dir_name(self) -> None:
        title, year = pipeline.parse_movie_query(QUERY)
        assert pipeline.slugify(f"{title}-{year}") == SLUG

    def test_cache_lookups_resolve_against_the_real_repo(
        self, live_settings: Settings, wordsman_root: Path
    ) -> None:
        assert pipeline.resolve_repo_data_dir(live_settings) == wordsman_root / "data"


class TestSubtitleCacheLane:
    """data/in/odyssey-2026/** — the already-fetched subtitle."""

    def test_cached_subtitle_is_real_content(
        self, live_settings: Settings, cached_in: Path
    ) -> None:
        srt = pipeline.cached_srt(live_settings, SLUG)
        assert srt is not None, f"no cached subtitle resolved from {cached_in}"
        assert srt.parent == cached_in
        assert not pipeline._is_lfs_pointer(srt)
        assert "-->" in srt.read_text(encoding="utf-8", errors="replace")[:4096]

    def test_custom_prefs_regenerate_from_the_cached_subtitle(
        self, live_settings: Settings, cached_in: Path
    ) -> None:
        """Non-default prefs bypass data/out and really run subtitle-dict on data/in's SRT.

        Slow (a real generation) but offline: proves the cached subtitle alone is enough
        to serve a user whose settings make the prebuilt wordlists inapplicable.
        """
        get_store(live_settings).set(USER, top=FAST_TOP, formats_exclude=FAST_EXCLUDE)
        client = _client(live_settings)
        response = client.post(
            "/api/v1/wordlists/movie", json={"title": TITLE, "year": YEAR, "user_id": USER}
        )
        assert response.status_code == 200, response.text
        # Exactly the two kept formats — nothing from the 14-format prebuilt data/out tree.
        assert _zip_names(response) == {"quizlet.tsv", "subtitle-dictionary.md"}

    def test_regenerated_wordlists_have_real_content(
        self, live_settings: Settings, cached_in: Path
    ) -> None:
        """The regenerated archive must carry actual entries, not empty format stubs."""
        get_store(live_settings).set(USER, top=FAST_TOP, formats_exclude=FAST_EXCLUDE)
        response = _client(live_settings).post(
            "/api/v1/wordlists/movie", json={"title": TITLE, "year": YEAR, "user_id": USER}
        )
        assert response.status_code == 200, response.text
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            quizlet = archive.read("quizlet.tsv").decode("utf-8")
        rows = [line for line in quizlet.splitlines() if line.strip()]
        assert rows, "quizlet.tsv is empty"
        assert rows[0].startswith("Term\t"), rows[0]  # header, then one row per term
        terms = rows[1:]
        assert terms, "quizlet.tsv holds only a header"
        assert len(terms) <= FAST_TOP, f"top={FAST_TOP} was ignored: {len(terms)} terms"
        assert all("\t" in row for row in terms), terms[:3]


class TestWordlistCacheLane:
    """data/out/odyssey-2026/** — the prebuilt wordlists, served without regeneration."""

    def test_movie_endpoint_returns_a_zip(self, client: TestClient) -> None:
        response = client.post("/api/v1/wordlists/movie", json={"title": TITLE, "year": YEAR})
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/zip"

    def test_zip_holds_exactly_the_prebuilt_tree(
        self, client: TestClient, prebuilt_out: Path
    ) -> None:
        response = client.post("/api/v1/wordlists/movie", json={"title": TITLE, "year": YEAR})
        assert response.status_code == 200, response.text
        expected = {str(p.relative_to(prebuilt_out)) for p in _usable_files(prebuilt_out)}
        assert _zip_names(response) == expected

    def test_zip_bytes_match_the_prebuilt_files(
        self, client: TestClient, prebuilt_out: Path
    ) -> None:
        response = client.post("/api/v1/wordlists/movie", json={"title": TITLE, "year": YEAR})
        assert response.status_code == 200, response.text
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            for path in _usable_files(prebuilt_out):
                name = str(path.relative_to(prebuilt_out))
                assert archive.read(name) == path.read_bytes(), f"{name} differs from the cache"

    def test_zip_carries_recognisable_wordlist_formats(self, client: TestClient) -> None:
        response = client.post("/api/v1/wordlists/movie", json={"title": TITLE, "year": YEAR})
        stems = {Path(n).stem for n in _zip_names(response)}
        assert stems & {"quizlet", "anki", "mochi"}, sorted(stems)

    def test_full_query_string_reaches_the_same_cache(self, client: TestClient) -> None:
        """`parse_movie_query` is the bot's job — the API takes title+year already split."""
        title, year = pipeline.parse_movie_query(QUERY)
        response = client.post("/api/v1/wordlists/movie", json={"title": title, "year": year})
        assert response.status_code == 200, response.text
        assert _zip_names(response)


@pytest.mark.network
class TestLiveFetchLane:
    """No cache at all: the providers must still be able to serve this title."""

    async def test_live_fetch_lands_a_subtitle(
        self, nocache_settings: Settings, tmp_path: Path
    ) -> None:
        """Reproduces the data/in/<slug>/*.srt shape from the network."""
        srt = await pipeline.fetch_srt(LIVE_TITLE, YEAR, nocache_settings, tmp_path / "srt")
        assert srt.is_file() and srt.suffix == ".srt"
        assert srt.stat().st_size > 1024, f"{srt} is suspiciously small"
        assert "-->" in srt.read_text(encoding="utf-8", errors="replace")[:4096]

    def test_movie_endpoint_without_cache(self, nocache_settings: Settings) -> None:
        """Reproduces the data/out/<slug>/** shape end-to-end, network included."""
        response = _client(nocache_settings).post(
            "/api/v1/wordlists/movie", json={"title": LIVE_TITLE, "year": YEAR}
        )
        assert response.status_code == 200, response.text
        names = _zip_names(response)
        assert names, "live pipeline produced an empty archive"
        assert any(n.endswith(".md") for n in names), sorted(names)
