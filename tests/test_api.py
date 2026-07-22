"""FastAPI endpoint tests against the fake wordsman checkout (no network)."""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from tg_bot.api import create_app
from tg_bot.config import Settings, get_settings


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def test_healthz(client: TestClient) -> None:
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["service"] == "tg-bot"


def test_movie_returns_zip(client: TestClient) -> None:
    response = client.post("/api/v1/wordlists/movie", json={"title": "Dune", "year": 2021})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert "subtitle-dictionary.md" in names


def test_movie_not_found(client: TestClient) -> None:
    response = client.post("/api/v1/wordlists/movie", json={"title": "Nonexistent"})
    assert response.status_code == 404
    assert "No subtitles found" in response.json()["detail"]


def test_movie_validation(client: TestClient) -> None:
    assert client.post("/api/v1/wordlists/movie", json={"title": ""}).status_code == 422


def test_document_returns_zip(client: TestClient) -> None:
    response = client.post(
        "/api/v1/wordlists/document",
        files={"file": ("story.txt", b"many fine words", "text/plain")},
    )
    assert response.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert "subtitle-dictionary.md" in names


def test_document_unsupported_type(client: TestClient) -> None:
    response = client.post(
        "/api/v1/wordlists/document",
        files={"file": ("movie.docx", b"x", "application/octet-stream")},
    )
    assert response.status_code == 415


def test_document_too_large(client: TestClient, settings: Settings) -> None:
    settings.max_document_mb = 0.00001  # ~10 bytes
    response = client.post(
        "/api/v1/wordlists/document",
        files={"file": ("story.txt", b"x" * 1024, "text/plain")},
    )
    assert response.status_code == 413


def test_document_pipeline_failure(client: TestClient) -> None:
    response = client.post(
        "/api/v1/wordlists/document",
        files={"file": ("bad.txt", b"EXPLODE", "text/plain")},
    )
    assert response.status_code == 502


def test_movie_unexpected_error_surfaces_real_text(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("disk is on fire")

    monkeypatch.setattr("tg_bot.api.pipeline.movie_to_wordlists", boom)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/api/v1/wordlists/movie", json={"title": "Dune"})
    assert response.status_code == 500
    assert response.json()["detail"] == "RuntimeError: disk is on fire"
