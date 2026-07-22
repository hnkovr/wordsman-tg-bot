"""FastAPI app: movie name or uploaded document in, ZIP of wordsman wordlists out."""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from tg_bot import __version__, pipeline
from tg_bot.config import Settings, get_settings
from tg_bot.logger import log
from tg_bot.models import MovieRequest
from tg_bot.pipeline import PipelineError, SubtitlesNotFoundError

_CHUNK = 1 << 20


def _zip_response(zip_path: Path) -> FileResponse:
    return FileResponse(zip_path, media_type="application/zip", filename=zip_path.name)


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
        try:
            zip_path = await pipeline.movie_to_wordlists(req.title, req.year, settings)
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
        file: UploadFile = File(...), settings: Settings = Depends(get_settings)
    ) -> FileResponse:
        name = Path(file.filename or "document.txt").name
        suffix = Path(name).suffix.lower()
        if suffix not in pipeline.SUPPORTED_SUFFIXES:
            allowed = ", ".join(sorted(pipeline.SUPPORTED_SUFFIXES))
            raise HTTPException(status_code=415, detail=f"Unsupported type; allowed: {allowed}")
        limit = int(settings.max_document_mb * 1024 * 1024)
        upload_dir = pipeline._work_dir(settings, f"upload-{pipeline.slugify(name)}")
        saved = upload_dir / name
        size = 0
        with saved.open("wb") as sink:
            while chunk := await file.read(_CHUNK):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Document exceeds {settings.max_document_mb:g} MB limit",
                    )
                sink.write(chunk)
        log.info("received document {} ({} bytes)", name, size)
        try:
            zip_path = await pipeline.document_to_wordlists(saved, settings, title=Path(name).stem)
        except PipelineError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:  # surface the real text, never a bare 500
            log.exception("unexpected failure for document {!r}", name)
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
        return _zip_response(zip_path)

    return app


app = create_app()
