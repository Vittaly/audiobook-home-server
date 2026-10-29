"""
Онлайн-поиск обложек книг (томов).

Отличается от обложки карточки-раздачи (_ensure_cover в ingest):
  - ищет по названию КНИГИ (если у книги своё название);
  - запускается в фоне из поллера, когда у многотомной книги нет своей
    обложки, а файл-картинка из торрента либо отсутствует в структуре,
    либо уже не появится (книга готова, а файла нет);
  - уважает COVER_RETRY_LIMIT и не запускается повторно, пока идёт
    текущий поиск (защита от дублей на стороне поллера через in-flight set).

Вызывается из фоновой asyncio-задачи поллера с ОТКРЫТОЙ сессией:
  - book должен быть persistent (attached к сессии);
  - функция мутирует book.cover_*, коммит делает вызывающий (session_scope).

После успешного поиска/скачивания:
  - book.cover_* заполняется через CoverService.fetch_from_url;
  - cover_origin = "online";
  - cover_is_temp = False — клиент перестаёт видеть обложку раздачи.
"""

import logging
import re

from sqlalchemy import select

from app.catalog.covers import CoverService
from app.config import settings
from app.db.models import Book, Release
from app.db.session import session_scope

log = logging.getLogger(__name__)


async def search_book_cover_detached(covers: CoverService, book_hash: str) -> bool:
    """
    Онлайн-поиск обложки книги БЕЗ открытой БД-транзакции.

    Порядок:
      1. Короткая транзакция: читаем книгу, проверяем актуальность,
         фиксируем title/author для поиска.
      2. Сеть (поиск + скачивание) — база данных НЕ залочена.
      3. Короткая транзакция: применяем результат (cover_*, cover_is_temp,
         cover_retry_count).

    Иначе во время сети (может занимать секунды) поллер не смог бы писать
    в БД — "database is locked". Вызывается из фоновой задачи поллера.
    """
    from app.catalog.cover_search import search_cover

    # ---------- 1. Короткая транзакция: читаем книгу ----------
    with session_scope() as db:
        book = db.execute(
            select(Book).where(Book.book_hash == book_hash)
        ).scalar_one_or_none()
        if book is None:
            return False

        # повторная проверка: обложка могла появиться (файл докачался,
        # другой проход поллера уже снял temp)
        if book.cover_cache_path is not None or not book.cover_is_temp:
            return False
        if (book.cover_retry_count or 0) >= settings.COVER_RETRY_LIMIT:
            return False
        if not settings.COVER_SEARCH_ENABLED:
            return False

        release = book.releases[0] if book.releases else None
        if release is None:
            return False

        title = _clean_book_title(book.title or release.title)
        author = release.release_author

    # ---------- 2. Сеть: база данных НЕ залочена ----------
    result = None
    try:
        log.info("book=%s: ищем обложку в интернете (%s — %s)",
                 book_hash[:12], author, title)
        result = await search_cover(title, author)
    except Exception:
        log.exception("book=%s: сбой поиска обложки", book_hash[:12])

    meta = None
    try:
        if result is not None:
            meta = await covers.fetch_url_to_cache(result.url)
    except Exception:
        log.exception("book=%s: сбой скачивания обложки %s",
                      book_hash[:12], result.url if result else "?")
        meta = None

    # ---------- 3. Короткая транзакция: применяем результат ----------
    with session_scope() as db:
        book = db.get(Book, book_hash)
        if book is None:
            return False

        if meta is not None:
            book.cover_origin = "online"
            book.cover_cache_path = meta["cache_path"]
            book.cover_sha256 = meta["sha256"]
            book.cover_mime = meta["mime"]
            book.cover_is_temp = False
            book.cover_retry_count = 0
            if result is not None:
                log.info("book=%s: обложка из %s (%s)",
                         book_hash[:12], result.source, result.url)
            return True

        if result is not None:
            log.warning("book=%s: не скачана обложка из %s (%s)",
                        book_hash[:12], result.source, result.url)
        else:
            log.warning("book=%s: обложка не найдена в интернете",
                        book_hash[:12])
        book.cover_retry_count = (book.cover_retry_count or 0) + 1
        return False


async def try_search_book_cover(
    covers: CoverService, book: Book, release: Release,
) -> bool:
    """
    Пытается найти обложку книги в интернете и сохранить её.

    book должен быть attached к открытой сессии — обновлённые поля
    (cover_*, cover_is_temp, cover_retry_count) закоммитит вызывающий.

    Возвращает True, если обложка получена.
    """
    if book.cover_cache_path is not None:
        return False

    if (book.cover_retry_count or 0) >= settings.COVER_RETRY_LIMIT:
        log.debug("book=%s: исчерпан лимит ретраев обложки (%d)",
                  book.book_hash[:12], settings.COVER_RETRY_LIMIT)
        return False

    if not settings.COVER_SEARCH_ENABLED:
        return False

    from app.catalog.cover_search import search_cover

    title = _clean_book_title(book.title or release.title)
    author = release.release_author

    log.info("book=%s: ищем обложку в интернете (%s — %s)",
             book.book_hash[:12], author, title)
    try:
        result = await search_cover(title, author)
    except Exception:
        log.exception("book=%s: сбой поиска обложки", book.book_hash[:12])
        result = None

    if result is None:
        log.warning("book=%s: обложка не найдена в интернете",
                    book.book_hash[:12])
        _bump_retry(book)
        return False

    try:
        ok = await covers.fetch_from_url(book, result.url)
    except Exception:
        log.exception("book=%s: сбой скачивания обложки %s",
                      book.book_hash[:12], result.url)
        ok = False

    if not ok:
        log.warning("book=%s: не скачана обложка из %s (%s)",
                    book.book_hash[:12], result.source, result.url)
        _bump_retry(book)
        return False

    book.cover_origin = "online"
    book.cover_is_temp = False
    book.cover_retry_count = 0
    log.info("book=%s: обложка из %s (%s)",
             book.book_hash[:12], result.source, result.url)
    return True


def _bump_retry(book: Book) -> None:
    """Увеличивает счётчик ретраев (сохранится при коммите сессии)."""
    book.cover_retry_count = (book.cover_retry_count or 0) + 1


def _clean_book_title(title: str) -> str:
    """
    'Автор - Название' → 'Название'; отрезает суффиксы томов
    ('Том 1', 'Книга 2' и т.п.) — поиску нужна чистая серия/название.
    """
    text = title.strip()
    if " - " in text:
        text = text.split(" - ", 1)[1].strip()
    # отрезаем 'Том 1' / 'Книга 2' / 'Часть 3' на конце
    text = re.sub(
        r"[\s,.:;_\-–—]*(?:том|книга|часть|выпуск|т\.|кн\.|в\.)"
        r"\s*[№#]?\s*\d+\s*$",
        "", text, flags=re.IGNORECASE,
    ).strip()
    return text

