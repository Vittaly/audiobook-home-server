from datetime import datetime

from pydantic import BaseModel

from app.schemas.chapter import ChapterOut
from app.schemas.person import PersonRef
from app.schemas.series import SeriesRef


class ReleaseBookOut(BaseModel):
    """Книга (том) внутри карточки раздачи — краткое представление."""
    id: int
    ordinal: int
    title: str
    folder: str | None = None
    series: SeriesRef | None = None
    status: str
    chapters_count: int = 0
    chapters_ready: int = 0
    total_duration_sec: int | None = None
    size_bytes: int | None = None
    # признак «книга ещё не полностью определена»: данные книги могут измениться.
    is_provisional: bool = False
    cover_url: str | None = None
    cover_is_temp: bool = False
    last_accessed: datetime | None = None


class ReleaseBookDetailOut(ReleaseBookOut):
    """Книга (том) внутри карточки раздачи с главами."""
    chapters: list[ChapterOut] = []


class ReleaseOut(BaseModel):
    """Карточка раздачи для списков."""
    id: int
    title: str
    title_raw: str | None = None
    author: str | None = None
    narrator: str | None = None
    year: int | None = None
    language: str | None = None
    description: str | None = None
    rubrics: list[str] = []
    status: str
    content_type: str = "single"
    books_count: int = 0
    books_ready: int = 0
    chapters_count: int = 0
    chapters_ready: int = 0
    total_duration_sec: int | None = None
    size_bytes: int | None = None
    cover_url: str | None = None
    # added_at — время индексации на сервере; released_at — дата создания
    # раздачи на трекере (сортировка «сначала новые» идёт по ней).
    added_at: datetime
    released_at: datetime
    downloaded_at: datetime | None = None
    last_accessed: datetime | None = None


class ReleaseDetailOut(ReleaseOut):
    """Полная карточка раздачи с персонами и книгами (2+ уровня)."""
    persons: list[PersonRef] = []
    books: list[ReleaseBookDetailOut] = []