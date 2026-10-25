from __future__ import annotations

from pydantic import BaseModel


class ChapterOut(BaseModel):
    """Глава книги. Одна запись = один аудиофайл."""
    id: int
    ordinal: int
    title: str | None
    duration_sec: int | None
    size_bytes: int
    status: str
    progress: float
    stream_url: str