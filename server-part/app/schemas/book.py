from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.schemas.chapter import ChapterOut
from app.schemas.person import PersonRef
from app.schemas.series import SeriesRef


class BookOut(BaseModel):
    """Книга (том) для списков по всему каталогу."""
    id: int
    ordinal: int = 1
    title: str
    author: str | None = None
    narrator: str | None = None
    year: int | None = None
    language: str | None = None

    series: SeriesRef | None = None
    rubrics: list[str] = []

    status: str
    chapters_count: int = 0
    chapters_ready: int = 0
    total_duration_sec: int | None = None
    size_bytes: int | None = None

    # признак «книга ещё не полностью определена»: данные книги (главы,
    # описание) могут измениться; снимается после получения глав/описания.
    is_provisional: bool = False

    cover_url: str | None = None
    cover_is_temp: bool = False

    added_at: datetime
    last_accessed: datetime | None = None


class BookDetailOut(BookOut):
    """Полная карточка книги с главами, персонами и данными карточки раздачи."""
    title_raw: str | None = None
    description: str | None = None
    persons: list[PersonRef] = []
    chapters: list[ChapterOut] = []