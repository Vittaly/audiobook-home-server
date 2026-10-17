"""
Обновление активности книги при обращениях.

Вызывается из роутов, которые «используют» книгу:
  - GET  /books/{id}/stream/{chapter_id}  → action='stream'
  - POST /books/{id}/download             → action='download'
  - GET  /books/{id}                      → action='detail' (без TTL-продления)
  - GET  /books                           → action='list'   (без TTL-продления)
  - DELETE /books/{id}                    → action='delete'

Правило:
  - 'stream' и 'download' продлевают TTL: обновляют book.last_accessed
    и chapter.last_streamed_at;
  - остальные действия только пишутся в access_log для аналитики,
    last_accessed не трогают.

Функция touch() безопасна: если книга не найдена или БД недоступна —
логирует и возвращается, не роняя вызывающий код.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import AccessLog, Book

log = logging.getLogger(__name__)

# какие действия продлевают срок жизни книги
TTL_ACTIONS = frozenset({"stream", "download"})

# все допустимые действия (совпадает с CHECK в модели AccessLog)
ALL_ACTIONS = frozenset({
    "list", "detail", "stream", "download", "delete",
})


# ============================================================
# ПУБЛИЧНОЕ API
# ============================================================

def touch(db: Session, book_id: int, action: str) -> None:
    """
    Фиксирует обращение к книге.

    :param db:      SQLAlchemy-сессия (не коммитит — это делает вызывающий)
    :param book_id: ID книги
    :param action:  'stream' | 'download' | 'list' | 'detail' | 'delete'
    """
    if action not in ALL_ACTIONS:
        log.warning("activity.touch: неизвестный action=%r, "
                    "сохраняю как 'detail'", action)
        action = "detail"

    try:
        book = db.get(Book, book_id)
        if book is None:
            log.debug("activity.touch: book=%s не найден, пропуск", book_id)
            return

        now = datetime.now(timezone.utc)

        # --- TTL-продление ---
        if action in TTL_ACTIONS:
            book.last_accessed = now
            log.info("activity: book=%s action=%s → last_accessed=%s",
                     book_id, action, now.isoformat())

        # --- запись в access_log ---
        db.add(AccessLog(
            book_id=book_id,
            action=action,
            accessed_at=now,
        ))

    except Exception:
        # не даём упасть основному запросу из-за логирования активности
        log.exception("activity.touch: ошибка book=%s action=%s",
                      book_id, action)


def touch_chapter(db: Session, book_id: int, chapter_id: int,
                  action: str = "stream") -> None:
    """
    Отмечает обращение к конкретной главе.

    Отличие от touch():
      - пишет chapter_id в access_log;
      - при action='stream' обновляет chapter.last_streamed_at,
        чтобы TTL-очистка видела, какие главы слушают.
    """
    from app.db.models import Chapter

    try:
        book = db.get(Book, book_id)
        if book is None:
            log.debug("activity.touch_chapter: book=%s не найден", book_id)
            return

        chapter = db.get(Chapter, chapter_id)
        if chapter is None or chapter.book_id != book_id:
            log.debug("activity.touch_chapter: chapter=%s не найден "
                      "или не принадлежит book=%s", chapter_id, book_id)
            # всё равно пишем обращение к книге
            touch(db, book_id, action)
            return

        now = datetime.now(timezone.utc)

        if action in TTL_ACTIONS:
            book.last_accessed = now

        if action == "stream":
            chapter.last_streamed_at = now

        db.add(AccessLog(
            book_id=book_id,
            chapter_id=chapter_id,
            action=action,
            accessed_at=now,
        ))

        log.info("activity: book=%s chapter=%s action=%s",
                 book_id, chapter_id, action)

    except Exception:
        log.exception("activity.touch_chapter: ошибка book=%s chapter=%s",
                      book_id, chapter_id)


# ============================================================
# ОПЦИОНАЛЬНЫЕ ХЕЛПЕРЫ
# ============================================================

def is_recently_used(book: Book, days: int) -> bool:
    """
    Проверяет, использовалась ли книга за последние N дней.
    Удобно для cleanup_unused и аналитики.
    """
    if book.last_accessed is None:
        # ни разу не слушали — считаем «недавно использованной»,
        # чтобы случайно не удалить только что добавленную книгу
        return True

    delta = datetime.now(timezone.utc) - book.last_accessed.replace(
        tzinfo=timezone.utc) if book.last_accessed.tzinfo is None \
        else datetime.now(timezone.utc) - book.last_accessed

    return delta.days < days