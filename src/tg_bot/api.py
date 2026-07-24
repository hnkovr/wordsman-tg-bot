"""FastAPI app: movie name or uploaded document in, ZIP of wordsman wordlists out."""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from tg_bot import __version__, pipeline
from tg_bot.config import Settings, get_settings
from tg_bot.logger import log
from tg_bot.models import MovieRequest
from tg_bot.pipeline import PipelineError, SubtitlesNotFoundError
from tg_bot.store import PrefStore, effective_settings, get_store

_CHUNK = 1 << 20


def _zip_response(zip_path: Path) -> FileResponse:
    return FileResponse(zip_path, media_type="application/zip", filename=zip_path.name)


def _apply_user(
    settings: Settings, store: PrefStore, user_id: int | None
) -> tuple[Settings, str | None]:
    """Return (effective settings, work-dir scope) for a request, applying the user's prefs."""
    if user_id is None:
        return settings, None
    return effective_settings(settings, store.get(user_id)), str(user_id)


def create_app() -> FastAPI:
    app = FastAPI(
        title="wordsman-tg-bot API",
        version=__version__,
        description="Generate wordsman wordlists from a movie name or an uploaded document.",
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "tg-bot", "version": __version__}

    @app.post("/api/v1/wordlists/movie")
    async def wordlists_movie(
        req: MovieRequest, settings: Settings = Depends(get_settings)
    ) -> FileResponse:
        eff, scope = _apply_user(settings, get_store(settings), req.user_id)
        try:
            zip_path = await pipeline.movie_to_wordlists(req.title, req.year, eff, scope=scope)
        except SubtitlesNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PipelineError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:  # surface the real text, never a bare 500
            log.exception("unexpected failure for movie {!r}", req.title)
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
        return _zip_response(zip_path)

    @app.post("/api/v1/wordlists/document")
    async def wordlists_document(
        file: UploadFile = File(...),
        user_id: int | None = Form(None),
        settings: Settings = Depends(get_settings),
    ) -> FileResponse:
        eff, scope = _apply_user(settings, get_store(settings), user_id)
        name = Path(file.filename or "document.txt").name
        suffix = Path(name).suffix.lower()
        if suffix not in pipeline.SUPPORTED_SUFFIXES:
            allowed = ", ".join(sorted(pipeline.SUPPORTED_SUFFIXES))
            raise HTTPException(status_code=415, detail=f"Unsupported type; allowed: {allowed}")
        limit = int(eff.max_document_mb * 1024 * 1024)
        upload_dir = pipeline._work_dir(eff, f"upload-{pipeline.slugify(name)}", scope)
        saved = upload_dir / name
        size = 0
        with saved.open("wb") as sink:
            while chunk := await file.read(_CHUNK):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Document exceeds {eff.max_document_mb:g} MB limit",
                    )
                sink.write(chunk)
        log.info("received document {} ({} bytes)", name, size)
        try:
            zip_path = await pipeline.document_to_wordlists(
                saved, eff, title=Path(name).stem, scope=scope
            )
        except PipelineError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:  # surface the real text, never a bare 500
            log.exception("unexpected failure for document {!r}", name)
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
        return _zip_response(zip_path)

    return app


app = create_app()
