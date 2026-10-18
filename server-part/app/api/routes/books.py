import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.catalog import service
from app.db.models import Book, Release
from app.db.session import get_db
from app.schemas import (
    BookDetailOut, BookOut, ListResponse,
    ReleaseBookDetailOut, ReleaseDetailOut, ReleaseOut,
)

log = logging.getLogger(__name__)
router = APIRouter()


# ============================================================
# ПРЕОБРАЗОВАНИЕ ORM → СХЕМА
# ============================================================

def _author_of(release: Release) -> str | None:
    for rp in release.persons:
        if rp.role == "author":
            return rp.person.name
    return None


def _narrator_of(release: Release) -> str | None:
    for rp in release.persons:
        if rp.role == "narrator":
            return rp.person.name
    return None


def _rubrics_of(release: Release) -> list[str]:
    return [rr.rubric.title for rr in release.rubrics]


def _series_ref(book: Book) -> dict | None:
    if book.series:
        return {
            "id": book.series.id,
            "title": book.series.title,
            "order": book.series_order,
        }
    return None


def _chapters_to_out(book: Book) -> list[dict]:
    return [
        {
            "id": c.id,
            "ordinal": c.ordinal,
            "title": c.title,
            "duration_sec": c.duration_sec,
            "size_bytes": c.size_bytes,
            "status": c.status,
            "progress": round(c.progress, 3),
            "stream_url": f"/books/{book.id}/stream/{c.id}",
        }
        for c in book.chapters
    ]


def _release_book_to_out(book: Book, api_base: str) -> dict:
    chapters = list(book.chapters)
    duration = sum(c.duration_sec or 0 for c in chapters) or None
    size = sum(c.size_bytes for c in chapters) or None

    return {
        "id": book.id,
        "ordinal": book.ordinal,
        "title": book.title,
        "folder": book.folder,
        "series": _series_ref(book),
        "status": book.status,
        "chapters_count": len(chapters),
        "chapters_ready": sum(1 for c in chapters if c.status == "ready"),
        "total_duration_sec": duration,
        "size_bytes": size,
        # признак «книга ещё не полностью определена»: данные могут измениться
        "is_provisional": book.is_provisional,
        # cover_url всегда не-null: /books/{id}/cover отдаёт обложку или placeholder
        "cover_url": f"{api_base}/books/{book.id}/cover",
        "cover_is_temp": book.cover_is_temp,
        "last_accessed": book.last_accessed,
    }


def _release_to_out(release: Release, api_base: str) -> ReleaseOut:
    books = list(release.books)
    chapters = [c for b in books for c in b.chapters]
    duration = sum(c.duration_sec or 0 for c in chapters) or None
    size = sum(c.size_bytes for c in chapters) or None

    return ReleaseOut(
        id=release.id,
        title=release.title,
        title_raw=release.title_raw,
        author=_author_of(release),
        narrator=_narrator_of(release),
        year=release.year,
        language=release.language,
        description=release.description,
        rubrics=_rubrics_of(release),
        status=release.status,
        content_type=release.content_type,
        books_count=len(books),
        books_ready=sum(1 for b in books if b.status == "ready"),
        chapters_count=len(chapters),
        chapters_ready=sum(1 for c in chapters if c.status == "ready"),
        total_duration_sec=duration,
        size_bytes=size,
        # cover_url всегда не-null: /releases/{id}/cover отдаёт обложку или placeholder
        cover_url=f"{api_base}/releases/{release.id}/cover",
        added_at=release.added_at,
        released_at=release.released_at,
        downloaded_at=release.downloaded_at,
        last_accessed=release.last_accessed,
    )


def _release_detail(release: Release, api_base: str) -> ReleaseDetailOut:
    base = _release_to_out(release, api_base).model_dump()

    base["persons"] = [
        {"name": rp.person.name, "role": rp.role} for rp in release.persons
    ]
    base["books"] = [
        ReleaseBookDetailOut(
            **_release_book_to_out(b, api_base),
            chapters=_chapters_to_out(b),
        )
        for b in release.books
    ]
    return ReleaseDetailOut(**base)


def _book_to_out(book: Book, api_base: str) -> BookOut:
    release = book.release
    chapters = list(book.chapters)
    duration = sum(c.duration_sec or 0 for c in chapters) or None
    size = sum(c.size_bytes for c in chapters) or None

    return BookOut(
        id=book.id,
        ordinal=book.ordinal,
        title=book.title,
        author=_author_of(release),
        narrator=_narrator_of(release),
        year=release.year,
        language=release.language,
        series=_series_ref(book),
        rubrics=_rubrics_of(release),
        status=book.status,
        chapters_count=len(chapters),
        chapters_ready=sum(1 for c in chapters if c.status == "ready"),
        total_duration_sec=duration,
        size_bytes=size,
        is_provisional=book.is_provisional,
        cover_url=f"{api_base}/books/{book.id}/cover",
        cover_is_temp=book.cover_is_temp,
        added_at=release.added_at,
        last_accessed=book.last_accessed,
    )


def _book_detail(book: Book, api_base: str) -> BookDetailOut:
    base = _book_to_out(book, api_base).model_dump()

    release = book.release
    base["title_raw"] = book.title_raw
    base["description"] = release.description
    base["persons"] = [
        {"name": rp.person.name, "role": rp.role} for rp in release.persons
    ]
    base["chapters"] = _chapters_to_out(book)

    return BookDetailOut(**base)


# ============================================================
# РОУТЫ: РАЗДАЧИ (КАРТОЧКИ)
# ============================================================

@router.get("/releases", response_model=ListResponse[ReleaseOut])
def list_releases(
    request: Request,
    query: str | None = None,
    status: str | None = None,
    rubric: str | None = None,
    author: str | None = None,
    narrator: str | None = None,
    series_id: int | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    sort: str = "added_at",
    order: str = "desc",
    db: Session = Depends(get_db),
):
    api_base = str(request.base_url).rstrip("/")
    releases, total = service.list_releases(
        db,
        query=query,
        status=status,
        rubric=rubric,
        author=author,
        narrator=narrator,
        series_id=series_id,
        limit=limit,
        offset=offset,
        sort=sort,
        order=order,
    )
    return ListResponse[ReleaseOut](
        items=[_release_to_out(r, api_base) for r in releases],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/releases/{release_id}", response_model=ReleaseDetailOut)
def get_release(
    release_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    release = service.get_release(db, release_id)
    if release is None:
        raise HTTPException(404, "release not found")

    api_base = str(request.base_url).rstrip("/")
    log.debug("get_release: id=%d", release_id)
    return _release_detail(release, api_base)


# ============================================================
# РОУТЫ: КНИГИ (ТОМА)
# ============================================================

@router.get("/books", response_model=ListResponse[BookOut])
def list_books(
    request: Request,
    query: str | None = None,
    status: str | None = None,
    rubric: str | None = None,
    author: str | None = None,
    narrator: str | None = None,
    series_id: int | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    sort: str = "added_at",
    order: str = "desc",
    db: Session = Depends(get_db),
):
    api_base = str(request.base_url).rstrip("/")
    books, total = service.list_books(
        db,
        query=query,
        status=status,
        rubric=rubric,
        author=author,
        narrator=narrator,
        series_id=series_id,
        limit=limit,
        offset=offset,
        sort=sort,
        order=order,
    )
    return ListResponse[BookOut](
        items=[_book_to_out(b, api_base) for b in books],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/books/{book_id}", response_model=BookDetailOut)
def get_book(
    book_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    book = service.get_book(db, book_id)
    if book is None:
        raise HTTPException(404, "book not found")

    api_base = str(request.base_url).rstrip("/")
    log.debug("get_book: id=%d", book_id)
    return _book_detail(book, api_base)