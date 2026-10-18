import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Book, Chapter, Person, Release, ReleasePerson, ReleaseRubric,
    Rubric, Series,
)

log = logging.getLogger(__name__)


# ============================================================
# РАЗДАЧИ (КАРТОЧКИ)
# ============================================================

def list_releases(db: Session, *,
                  query: str | None = None,
                  status: str | None = None,
                  rubric: str | None = None,
                  author: str | None = None,
                  narrator: str | None = None,
                  series_id: int | None = None,
                  limit: int = 50, offset: int = 0,
                  sort: str = "added_at",
                  order: str = "desc") -> tuple[list[Release], int]:
    """Карточки раздач. Каждая карточка содержит 1..N книг (Book)."""
    stmt = select(Release)

    if query:
        stmt = stmt.where(Release.title_norm.like(f"{query.lower()}%"))
    if status:
        stmt = stmt.where(Release.status == status)
    if series_id:
        stmt = stmt.join(Release.books).where(Book.series_id == series_id)
    if rubric:
        stmt = stmt.join(ReleaseRubric).join(
            Rubric, Rubric.id == ReleaseRubric.rubric_id
        ).where(Rubric.title_norm == rubric.lower())
    if author:
        stmt = stmt.join(ReleasePerson).join(
            Person, Person.id == ReleasePerson.person_id
        ).where(
            ReleasePerson.role == "author",
            Person.name_norm == author.lower(),
        )
    if narrator:
        stmt = stmt.join(ReleasePerson).join(
            Person, Person.id == ReleasePerson.person_id
        ).where(
            ReleasePerson.role == "narrator",
            Person.name_norm == narrator.lower(),
        )
    if series_id or rubric or author or narrator:
        # join могли размножить строки — считаем distinct
        stmt = stmt.distinct()

    total = db.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()

    sort_col = {
        "added_at":    Release.added_at,
        "released_at": Release.released_at,
        "title":       Release.title,
        "year":        Release.year,
    }.get(sort, Release.added_at)
    if order == "desc":
        sort_col = sort_col.desc()

    stmt = stmt.order_by(sort_col).limit(limit).offset(offset)
    stmt = stmt.options(
        selectinload(Release.books).selectinload(Book.series),
        selectinload(Release.persons),
        selectinload(Release.rubrics),
    )

    releases = list(db.execute(stmt).scalars().all())
    return releases, total


def get_release(db: Session, release_id: int) -> Release | None:
    """Полная карточка раздачи со всеми книгами и главами."""
    return db.execute(
        select(Release).where(Release.id == release_id)
        .options(
            selectinload(Release.books).selectinload(Book.chapters),
            selectinload(Release.books).selectinload(Book.series),
            selectinload(Release.persons),
            selectinload(Release.rubrics),
        )
    ).scalar_one_or_none()


# ============================================================
# КНИГИ (ТОМА)
# ============================================================

def list_books(db: Session, *,
               query: str | None = None,
               status: str | None = None,
               rubric: str | None = None,
               author: str | None = None,
               narrator: str | None = None,
               series_id: int | None = None,
               limit: int = 50, offset: int = 0,
               sort: str = "added_at",
               order: str = "desc") -> tuple[list[Book], int]:
    """Книги (тома) по всему каталогу. Поля карточки берутся из раздачи."""
    stmt = select(Book).join(Book.release)

    if query:
        stmt = stmt.where(Book.title_norm.like(f"{query.lower()}%"))
    if status:
        stmt = stmt.where(Book.status == status)
    if series_id:
        stmt = stmt.where(Book.series_id == series_id)
    if rubric:
        stmt = stmt.join(ReleaseRubric).join(
            Rubric, Rubric.id == ReleaseRubric.rubric_id
        ).where(Rubric.title_norm == rubric.lower())
    if author:
        stmt = stmt.join(ReleasePerson).join(
            Person, Person.id == ReleasePerson.person_id
        ).where(
            ReleasePerson.role == "author",
            Person.name_norm == author.lower(),
        )
    if narrator:
        stmt = stmt.join(ReleasePerson).join(
            Person, Person.id == ReleasePerson.person_id
        ).where(
            ReleasePerson.role == "narrator",
            Person.name_norm == narrator.lower(),
        )
    if rubric or author or narrator:
        stmt = stmt.distinct()

    total = db.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()

    sort_col = {
        "added_at":    Release.added_at,
        "released_at": Release.released_at,
        "title":       Book.title,
        "year":        Release.year,
    }.get(sort, Release.added_at)
    if order == "desc":
        sort_col = sort_col.desc()

    stmt = stmt.order_by(sort_col).limit(limit).offset(offset)
    stmt = stmt.options(
        selectinload(Book.release).selectinload(Release.persons),
        selectinload(Book.release).selectinload(Release.rubrics),
        selectinload(Book.series),
    )

    books = list(db.execute(stmt).scalars().all())
    return books, total


def get_book(db: Session, book_id: int) -> Book | None:
    """Книга с главами и данными карточки раздачи."""
    return db.execute(
        select(Book).where(Book.id == book_id)
        .options(
            selectinload(Book.chapters),
            selectinload(Book.series),
            selectinload(Book.release).selectinload(Release.persons),
            selectinload(Book.release).selectinload(Release.rubrics),
        )
    ).scalar_one_or_none()


def get_chapters(db: Session, book_id: int) -> list[Chapter]:
    return list(db.execute(
        select(Chapter).where(Chapter.book_id == book_id)
        .order_by(Chapter.ordinal)
    ).scalars().all())


def get_chapter_full(db: Session, chapter_id: int) -> Chapter | None:
    return db.execute(
        select(Chapter).where(Chapter.id == chapter_id)
        .options(
            selectinload(Chapter.release),
            selectinload(Chapter.book),
        )
    ).scalar_one_or_none()


# ============================================================
# СТАТУС ЗАГРУЗКИ
# ============================================================

def get_download_status(db: Session, book_id: int) -> dict | None:
    """Сводка прогресса загрузки книги: БД + торрент-клиент."""
    from app.torrents import manager

    book = get_book(db, book_id)
    if book is None:
        return None

    release = book.release

    chapters_total = len(book.chapters)
    chapters_ready = sum(1 for c in book.chapters if c.status == "ready")
    total_bytes = sum(c.size_bytes for c in book.chapters) or 0
    downloaded_bytes = sum(c.downloaded_bytes for c in book.chapters)
    progress = (downloaded_bytes / total_bytes) if total_bytes else 0.0

    speed_bps = 0
    eta_sec = None
    peers = 0
    seeds = 0
    client_state = None

    if release is not None:
        torrent = manager.get_torrent(release.torrent_hash)
        if torrent is not None:
            speed_bps = torrent.rate_download
            eta_sec = torrent.eta_sec
            peers = torrent.peers
            seeds = torrent.seeds
            client_state = torrent.status
            if not book.chapters:
                progress = torrent.progress

    return {
        "book_id": book.id,
        "release_id": release.id if release is not None else None,
        "status": book.status,
        "client_state": client_state,
        "progress": round(progress, 3),
        "total_bytes": total_bytes,
        "downloaded_bytes": downloaded_bytes,
        "speed_bps": speed_bps,
        "eta_sec": eta_sec,
        "peers": peers,
        "seeds": seeds,
        "chapters_ready": chapters_ready,
        "chapters_total": chapters_total,
        "chapters": [
            {
                "id": c.id, "ordinal": c.ordinal, "title": c.title,
                "status": c.status, "progress": round(c.progress, 3),
                "size_bytes": c.size_bytes,
                "downloaded_bytes": c.downloaded_bytes,
            }
            for c in book.chapters
        ],
    }


# ============================================================
# ОБЛОЖКИ
# ============================================================

def resolve_cover_url(owner, api_base: str) -> str:
    """Стабильный URL обложки: /releases/{id}/cover или /books/{id}/cover."""
    from app.db.models import Book, Release

    if isinstance(owner, Release):
        return f"{api_base}/releases/{owner.id}/cover"
    if isinstance(owner, Book):
        return f"{api_base}/books/{owner.id}/cover"
    kind = type(owner).__name__
    return f"{api_base}/{kind.lower()}s/{owner.id}/cover"