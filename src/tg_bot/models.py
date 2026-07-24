"""Pydantic request models for the wordlists API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class MovieRequest(BaseModel):
    title: str = Field(..., min_length=1, description="Movie title to search subtitles for")
    year: int | None = Field(None, ge=1888, description="Release year filter")
    user_id: int | None = Field(None, description="Telegram user id; applies that user's prefs")
