"""Movie/document → wordlists pipeline, reusing the parent wordsman checkout via subprocess.

The heavy lifting stays in the parent repo: `scripts/fetch_srt.sh` (movie → SRT) and
`main.py subtitle-dict` (SRT/text → export formats). This module only orchestrates,
extracts plain text from PDF/HTML uploads, and zips the results.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path

from tg_bot.config import REPO_ROOT, Settings
from tg_bot.logger import log

#: Every format `main.py subtitle-dict --formats` understands (see wordsman/cli.py).
SUPPORTED_FORMATS = (
    "md",
    "tsv",
    "json",
    "yaml",
    "sparsed-yaml",
    "sparsed-json",
    "quizlet",
    "anki",
    "flashcards-deluxe",
    "wokabulary",
    "mochi",
    "obsidian",
    "logseq",
    "flashbuddy",
)
TEXT_SUFFIXES = {".srt", ".vtt", ".txt", ".md"}
HTML_SUFFIXES = {".html", ".htm"}
PDF_SUFFIXES = {".pdf"}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | HTML_SUFFIXES | PDF_SUFFIXES

# Plausible release years only (1900-2029), so titles like "Blade Runner 2049" survive.
_YEAR_RE = re.compile(r"^(?P<title>.+?)[\s(]+(?P<year>19\d{2}|20[0-2]\d)\)?\s*$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class PipelineError(RuntimeError):
    """Pipeline failure carrying a user-safe message."""


class SubtitlesNotFoundError(PipelineError):
    """No subtitles could be fetched for the requested movie."""


def resolve_formats(settings: Settings) -> str:
    """All supported formats minus the configured short exception list."""
    excluded = {f.strip().lower() for f in settings.formats_exclude}
    unknown = excluded - set(SUPPORTED_FORMATS)
    if unknown:
        raise PipelineError(f"formats_exclude lists unknown formats: {sorted(unknown)}")
    kept = [f for f in SUPPORTED_FORMATS if f not in excluded]
    if not kept:
        raise PipelineError("formats_exclude excludes every supported format")
    return ",".join(kept)


def parse_movie_query(text: str) -> tuple[str, int | None]:
    """Split "Dune (2021)" / "Dune 2021" / "Dune" into (title, year|None)."""
    cleaned = " ".join(text.split())
    match = _YEAR_RE.match(cleaned)
    if match:
        return match.group("title").strip(" ("), int(match.group("year"))
    return cleaned, None


def slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug or "wordlists"


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self._chunks.append(data.strip())

    def text(self) -> str:
        return "\n".join(self._chunks)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text()


def pdf_to_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise PipelineError("PDF support requires the pypdf package") from exc
    try:
        reader = PdfReader(path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError(f"Could not read PDF: {exc}") from exc


def extract_document_text(src: Path, dest_dir: Path) -> Path:
    """Turn an uploaded document into a file `subtitle-dict` accepts (.srt/.vtt/.txt/.md)."""
    suffix = src.suffix.lower()
    dest_dir.mkdir(parents=True, exist_ok=True)
    if suffix in TEXT_SUFFIXES:
        dest = dest_dir / src.name
        if src != dest:
            shutil.copyfile(src, dest)
        return dest
    if suffix in HTML_SUFFIXES:
        text = html_to_text(src.read_text(encoding="utf-8", errors="ignore"))
    elif suffix in PDF_SUFFIXES:
        text = pdf_to_text(src)
    else:
        allowed = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise PipelineError(f"Unsupported document type '{suffix}'. Supported: {allowed}")
    if not text.strip():
        raise PipelineError(f"No text could be extracted from {src.name}")
    dest = dest_dir / f"{src.stem}.txt"
    dest.write_text(text, encoding="utf-8")
    return dest


def resolve_wordsman_root(settings: Settings) -> Path:
    """Locate the parent wordsman checkout (submodule layout or TG_BOT_WORDSMAN_ROOT)."""
    candidates = []
    if settings.wordsman_root:
        candidates.append(Path(settings.wordsman_root))
    candidates.append(REPO_ROOT.parent.parent)  # <wordsman>/subproducts/tg-bot
    for root in candidates:
        if (root / "main.py").is_file() and (root / "scripts" / "fetch_srt.sh").is_file():
            return root
    raise PipelineError(
        "wordsman checkout not found; set TG_BOT_WORDSMAN_ROOT to a repo "
        "containing main.py and scripts/fetch_srt.sh"
    )


def resolve_except_list(settings: Settings) -> Path | None:
    if settings.except_list is None:
        return None
    path = Path(settings.except_list)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path if path.is_file() else None


def stderr_reason(stderr: str, code: int) -> str:
    """Pull the most useful one-liner out of a subprocess' stderr for a user-facing error.

    Prefers the last non-traceback-scaffolding line (e.g. the `ProviderError: …` message)
    over the raw tail; falls back to the exit code when stderr is empty.
    """
    meaningful = [
        line.strip()
        for line in stderr.splitlines()
        if line.strip() and not line.lstrip().startswith(('File "', "^", "Traceback", ")"))
    ]
    return meaningful[-1] if meaningful else f"exit code {code}"


async def _run(cmd: list[str], *, timeout: float, cwd: Path) -> tuple[int, str, str]:
    log.info("run: {}", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise PipelineError(f"'{cmd[0]}' timed out after {timeout:.0f}s") from None
    return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def resolve_repo_data_dir(settings: Settings) -> Path | None:
    """The wordsman checkout's data/ dir, or None when the repo cache is unusable here.

    The cached blobs live in Git-LFS, so a deploy host that never pulled the LFS objects
    has no cache — that is a normal degrade to a live fetch, not an error.
    """
    if not settings.use_repo_cache:
        log.debug("repo cache: disabled by settings (use_repo_cache=false)")
        return None
    if settings.repo_data_dir is not None:
        data_dir = Path(settings.repo_data_dir)
    else:
        try:
            data_dir = resolve_wordsman_root(settings) / "data"
        except PipelineError as exc:
            log.debug("repo cache: no wordsman root ({}) — live fetch", exc)
            return None
    if not data_dir.is_dir():
        log.warning(
            "repo cache: {} is absent (LFS not pulled?) — falling back to live fetch", data_dir
        )
        return None
    log.debug("repo cache: using {}", data_dir)
    return data_dir


def _is_lfs_pointer(path: Path) -> bool:
    """True when the file is an unfetched Git-LFS pointer rather than real content."""
    try:
        with path.open("rb") as handle:
            return handle.read(42).startswith(b"version https://git-lfs")
    except OSError:
        return False


def cached_srt(settings: Settings, slug: str) -> Path | None:
    """An already-fetched subtitle at data/in/<slug>/*.srt, or None."""
    data_dir = resolve_repo_data_dir(settings)
    if data_dir is None:
        return None
    candidate_dir = data_dir / "in" / slug
    log.debug("repo cache: probing {} for a cached .srt", candidate_dir)
    if not candidate_dir.is_dir():
        log.debug("repo cache: MISS — no {}", candidate_dir)
        return None
    for srt in sorted(candidate_dir.glob("*.srt")):
        if _is_lfs_pointer(srt):
            log.warning("repo cache: {} is an unfetched LFS pointer — skipping", srt.name)
            continue
        if srt.stat().st_size == 0:
            log.warning("repo cache: {} is empty — skipping", srt.name)
            continue
        log.info("repo cache HIT: reusing subtitle {} (skipping network fetch)", srt.name)
        return srt
    log.debug("repo cache: MISS — no usable .srt in {}", candidate_dir)
    return None


def cached_wordlists(settings: Settings, slug: str) -> Path | None:
    """An already-built wordlist dir at data/out/<slug>, or None.

    Only safe when the user takes the global defaults — per-user prefs (level, top,
    formats) would otherwise be silently ignored, since these files were built with the
    repo's own settings. The caller decides; this only reports what exists.
    """
    data_dir = resolve_repo_data_dir(settings)
    if data_dir is None:
        return None
    candidate_dir = data_dir / "out" / slug
    log.debug("repo cache: probing {} for prebuilt wordlists", candidate_dir)
    if not candidate_dir.is_dir():
        log.debug("repo cache: MISS — no {}", candidate_dir)
        return None
    files = [p for p in candidate_dir.iterdir() if p.is_file() and not _is_lfs_pointer(p)]
    if not files:
        log.debug("repo cache: MISS — {} holds no usable files", candidate_dir)
        return None
    log.info("repo cache HIT: {} prebuilt wordlist files in {}", len(files), candidate_dir)
    return candidate_dir


def uses_default_generation(settings: Settings, defaults: Settings) -> bool:
    """True when generation knobs are untouched, so prebuilt wordlists are equivalent."""
    same = (
        settings.min_level == defaults.min_level
        and settings.top == defaults.top
        and list(settings.formats_exclude) == list(defaults.formats_exclude)
    )
    if not same:
        log.debug("repo cache: user prefs differ from defaults — regenerating wordlists")
    return same


async def fetch_srt(title: str, year: int | None, settings: Settings, out_dir: Path) -> Path:
    """Fetch the best SRT via the parent repo's fetch_srt.sh (prints the path last)."""
    root = resolve_wordsman_root(settings)
    cmd = ["bash", str(root / "scripts" / "fetch_srt.sh"), "--movie", title, "--out", str(out_dir)]
    if year:
        cmd += ["--year", str(year)]
    # Pin the provider list explicitly: srt-search's own default is podnapisi-only, whose
    # host is DNS-dead (wordsman#22), so relying on it makes every fetch fail.
    providers = ",".join(p.strip() for p in settings.srt_providers if p.strip())
    if providers:
        cmd += ["--providers", providers]
    log.info(
        "live fetch: '{}'{} via providers [{}]", title, f" ({year})" if year else "", providers
    )
    code, stdout, stderr = await _run(cmd, timeout=settings.fetch_timeout, cwd=root)
    if code != 0:
        log.warning("fetch_srt failed ({}): {}", code, stderr[-500:])
        reason = stderr_reason(stderr, code)
        raise SubtitlesNotFoundError(f"No subtitles found for '{title}' — {reason}")
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    srt = Path(lines[-1]) if lines else None
    if srt is None or not srt.is_file():
        raise PipelineError(f"Subtitle fetch for '{title}' returned no file")
    return srt


async def build_wordlists(
    input_path: Path, out_dir: Path, settings: Settings, *, source_title: str
) -> list[Path]:
    """Run `main.py subtitle-dict` on input_path, returning the generated files."""
    root = resolve_wordsman_root(settings)
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(root / "main.py"),
        "subtitle-dict",
        str(input_path),
        str(out_dir),
        "--settings",
        str(root / "conf" / "settings.yml"),
        "--formats",
        resolve_formats(settings),
        "--top",
        str(settings.top),
        "--source-title",
        source_title,
        "--source-id",
        slugify(source_title),
    ]
    if settings.min_level:
        cmd += ["--min-level", settings.min_level]
    except_list = resolve_except_list(settings)
    if except_list:
        cmd += ["--except-list", str(except_list)]
    code, _stdout, stderr = await _run(cmd, timeout=settings.dict_timeout, cwd=root)
    if code != 0:
        log.error("subtitle-dict failed ({}): {}", code, stderr[-800:])
        raise PipelineError(f"Wordlist generation failed: {stderr_reason(stderr, code)}")
    files = sorted(p for p in out_dir.iterdir() if p.is_file())
    if not files:
        raise PipelineError("Wordlist generation produced no files")
    return files


def zip_dir(src_dir: Path, zip_path: Path) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(src_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(src_dir))
    return zip_path


def _work_dir(settings: Settings, slug: str, scope: str | None = None) -> Path:
    base = Path(settings.work_dir)
    if not base.is_absolute():
        base = REPO_ROOT / base
    # Namespace by scope (the requesting user) so concurrent requests for the same
    # title don't clobber each other's scratch directory.
    work = (base / slugify(scope) / slug) if scope else (base / slug)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    return work


async def movie_to_wordlists(
    title: str,
    year: int | None,
    settings: Settings,
    *,
    scope: str | None = None,
    defaults: Settings | None = None,
) -> Path:
    """Full movie flow: (cache | fetch) SRT → wordlists → single ZIP; returns the ZIP path.

    Prefers the wordsman repo's own cache — an already-fetched `data/in/<slug>` subtitle,
    and, when the caller's generation knobs are untouched, the prebuilt `data/out/<slug>`
    wordlists — falling back to a live fetch/generation whenever the cache cannot serve.
    """
    slug = slugify(f"{title}-{year}" if year else title)
    log.debug("movie flow: title={!r} year={} slug={} scope={}", title, year, slug, scope)
    work = _work_dir(settings, slug, scope)

    # Fully prebuilt wordlists are only equivalent when generation knobs are default.
    prebuilt = cached_wordlists(settings, slug)
    if prebuilt is not None and uses_default_generation(settings, defaults or Settings()):
        log.info("movie '{}' → serving {} from the repo cache (no generation)", title, prebuilt)
        return zip_dir(prebuilt, work / f"{slug}-wordlists.zip")

    srt = cached_srt(settings, slug)
    if srt is None:
        log.debug("no cached subtitle for {} — going to the network", slug)
        srt = await fetch_srt(title, year, settings, work / "srt")
    files = await build_wordlists(srt, work / "out", settings, source_title=title)
    log.info("movie '{}' → {} wordlist files", title, len(files))
    return zip_dir(work / "out", work / f"{slug}-wordlists.zip")


async def document_to_wordlists(
    doc_path: Path, settings: Settings, *, title: str, scope: str | None = None
) -> Path:
    """Full document flow: extract text → wordlists → single ZIP; returns the ZIP path."""
    slug = slugify(title)
    work = _work_dir(settings, slug, scope)
    prepared = extract_document_text(doc_path, work / "src")
    files = await build_wordlists(prepared, work / "out", settings, source_title=title)
    log.info("document '{}' → {} wordlist files", doc_path.name, len(files))
    return zip_dir(work / "out", work / f"{slug}-wordlists.zip")
