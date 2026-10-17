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

from app.catalog.covers import CoverService
from app.config import settings
from app.db.models import Book, Release

log = logging.getLogger(__name__)


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
                  book.id, settings.COVER_RETRY_LIMIT)
        return False

    if not settings.COVER_SEARCH_ENABLED:
        return False

    from app.catalog.cover_search import search_cover

    title = _clean_book_title(book.title or release.title)
    author = _release_author(release)

    log.info("book=%s: ищем обложку в интернете (%s — %s)",
             book.id, author, title)
    try:
        result = await search_cover(title, author)
    except Exception:
        log.exception("book=%s: сбой поиска обложки", book.id)
        result = None

    if result is None:
        log.warning("book=%s: обложка не найдена в интернете", book.id)
        _bump_retry(book)
        return False

    try:
        ok = await covers.fetch_from_url(book, result.url)
    except Exception:
        log.exception("book=%s: сбой скачивания обложки %s",
                      book.id, result.url)
        ok = False

    if not ok:
        log.warning("book=%s: не скачана обложка из %s (%s)",
                    book.id, result.source, result.url)
        _bump_retry(book)
        return False

    book.cover_origin = "online"
    book.cover_is_temp = False
    book.cover_retry_count = 0
    log.info("book=%s: обложка из %s (%s)", book.id, result.source, result.url)
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


def _release_author(release: Release) -> str:
    """Имя автора из персон раздачи."""
    for rp in release.persons:
        if rp.role == "author":
            return rp.person.name
    return ""