"""
Эндпоинты стриминга глав.

GET  /books/{book_id}/stream/{chapter_id}  — отдаёт файл главы с Range
HEAD /books/{book_id}/stream/{chapter_id}  — то же без тела

Побочный эффект GET: activity.touch_chapter обновляет
book.last_accessed и chapter.last_streamed_at, продлевая TTL.
HEAD не трогает активность — ExoPlayer дёргает его перед GET,
не хотим двойной записи в access_log.
"""

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.streaming import serve_chapter
from app.catalog import service
from app.db.session import get_db
from app.lifecycle import activity

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/books/{book_id}/stream/{chapter_id}")
def stream_chapter(
    book_id: int,
    chapter_id: int,
    request: Request,
    range_header: str | None = Header(None, alias="Range"),
    db: Session = Depends(get_db),
):
    """
    Отдаёт главу как аудиопоток с поддержкой HTTP Range.
    Продлевает TTL книги и главы через activity.touch_chapter.
    """
    chapter = service.get_chapter_full(db, chapter_id)
    if chapter is None or chapter.book_id != book_id:
        raise HTTPException(404, "chapter not found")

    # активность: обновляет book.last_accessed, chapter.last_streamed_at
    # и пишет запись в access_log с chapter_id
    activity.touch_chapter(db, book_id, chapter_id, action="stream")
    db.commit()

    log.info("stream: book=%s chapter=%s status=%s range=%s",
             book_id, chapter_id, chapter.status, range_header)

    return serve_chapter(chapter, range_header)


@router.head("/books/{book_id}/stream/{chapter_id}")
def stream_chapter_head(
    book_id: int,
    chapter_id: int,
    db: Session = Depends(get_db),
):
    """
    HEAD-запрос: те же заголовки, без тела.
    ExoPlayer использует перед воспроизведением для проверки Range.
    Активность не трогаем — GET сделает это сам.
    """
    chapter = service.get_chapter_full(db, chapter_id)
    if chapter is None or chapter.book_id != book_id:
        raise HTTPException(404, "chapter not found")

    return serve_chapter(chapter, None)