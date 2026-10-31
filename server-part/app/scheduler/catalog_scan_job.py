"""
Job: сканирование каталога (поиск новых книг на торрент-трекере).

Оркестрирует процесс:
  1. Поиск/создание карточки раздачи (Release.find_or_create)
  2. Сетевой запрос деталей (fetch_details)
  3. Применение деталей (release.apply_details)
  4. Добавление торрента в клиент, создание книг/глав
  5. Обложка карточки (cover: трекер → интернет)

Вся бизнес-логика — в методах моделей (Release, Book, Chapter, Cover).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.covers import CoverService
from app.config import settings
from app.db.models import BookStatus, ChapterStatus, Release
from app.db.models.crawl_state import CrawlState
from app.db.session import SessionLocal
from app.scheduler.base import Job
from app.scraper.state import CrawlStateStore
from app.scraper.types import TorrentSource
from app.torrents import manager, structure

if TYPE_CHECKING:
    from app.scraper.types import SearchItem, TorrentDetails

log = logging.getLogger(__name__)


@contextmanager
def _sync_session():
    """Контекстный менеджер сессии с повтором commit при database is locked.

    После успешного commit транзакция закрыта — блокировка SQLite свободна.
    Объекты остаются attached к сессии (expire_on_commit=False),
    lazy-relationship доступны.
    """
    from app.db.session import _run_with_retry
    db = SessionLocal()
    try:
        yield db
        _run_with_retry(db.commit)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ============================================================
# Обработка одной записи поисковой выдачи
# ============================================================


async def _process_item(source: TorrentSource, covers: CoverService,
                        sem: asyncio.Semaphore,
                        item: "SearchItem") -> int:
    """
    Парсер создаёт Release и книги. Далее поллер только обновляет статусы.

    Последовательность:
      1. Upsert карточки Release (короткая транзакция).
      2. Детальная страница трекера (сеть) — описание, рубрики, чтец.
      3. Применение деталей к Release (короткая транзакция).
      4. Добавление торрента в клиент — если файлы уже известны,
         сразу создаются книги/главы и запускается быстрая загрузка.
         Если метаданных ещё нет — поллер доделает позже.
      5. Обложка карточки — трекер → интернет.
    """
    from app.scraper.types import SearchItem

    # --- 1. Upsert карточки ---
    with _sync_session() as db:
        release = Release.find_or_create(db, _item_to_dict(item))
        if release is None:
            return 0
        need_details = (
            release.description is None and bool(item.source_url)
        )
        need_cover = release.cover_cache_path is None
        release_id = release.id

    # --- 2. Детальная страница (сеть) ---
    raw = None
    if need_details:
        raw = await _fetch_details(sem, source, str(item.source_url))

    # --- 3. Применение деталей ---
    if raw:
        with _sync_session() as db:
            release = db.get(Release, release_id)
            if release is not None:
                release.apply_details(db, _details_to_dict(raw))

    # --- 4. Добавить торрент в клиент ---
    try:
        client_id = await release.start_download()
        if not client_id:
            log.warning("release=%s: не удалось добавить торрент", release_id)
            return 0
    except Exception:
        log.exception("release=%s: сбой добавления торрента", release_id)
        return 0

    # --- 4a. Если файлы уже доступны — создать книги/главы сразу ---
    files = manager.get_files(release.torrent_hash)
    if files:
        torrent_name = _torrent_root_name(files, release.title)
        prepared = _prepare_files(files)

        # Короткая транзакция: только ingest_torrent
        book_hashes: list[str] = []
        with _sync_session() as db:
            release = db.get(Release, release_id)
            if release is None:
                return 0

            created = release.ingest_torrent(db, prepared, torrent_name, covers)
            if created:
                book_hashes = [b.book_hash for b in release.books if b.chapters]

        # Быстрый старт книг — каждая открывает свои короткие транзакции
        if book_hashes:
            for book_hash in book_hashes:
                await _fast_download_detached(covers, book_hash)
    else:
        log.info("release=%s: метаданные ещё не получены, "
                 "книги/главы создаст поллер", release_id)

    # --- 5. Обложка карточки ---
    if need_cover:
        await _ensure_cover_detached(sem, release_id, covers)

    return 1


def _item_to_dict(item: "SearchItem") -> dict:
    """SearchItem → dict для Release.find_or_create."""
    return {
        "torrent_hash": item.torrent_hash,
        "title": item.title,
        "magnet": item.magnet,
        "content_type": item.content_type,
        "source_url": item.source_url,
        "size_bytes": item.size_bytes,
        "books_hint": item.books_hint,
        "author": item.author,
        "narrator": item.narrator,
        "title_raw": item.title_raw,
        "added_at": item.added_at,
        "year": item.year,
    }


def _details_to_dict(details: "TorrentDetails") -> dict:
    """TorrentDetails → dict для Release.apply_details."""
    from app.scraper.types import TorrentDetails
    return {
        "description": details.description,
        "year": details.year,
        "narrator": details.narrator,
        "rubrics": details.rubrics,
        "cover_url": details.cover_url,
        "content_type": details.content_type,
        "books_hint": details.books_hint,
        "publisher": details.publisher,
        "format": details.format,
        "duration_sec": details.duration_sec,
        "series_info": details.series_info,
    }


# ============================================================
# ОБЛОЖКИ: ТРЕКЕР → ИНТЕРНЕТ
# ============================================================


async def _ensure_cover_detached(sem: asyncio.Semaphore, release_id: int,
                                 covers: CoverService) -> None:
    """
    Пытается получить обложку карточки раздачи БЕЗ долгой транзакции БД.
    """
    from app.catalog.cover_search import search_cover

    # --- 0. Короткая транзакция: читаем состояние карточки ---
    with _sync_session() as db:
        release = db.get(Release, release_id)
        if release is None:
            return
        if release.cover_cache_path is not None:
            return
        source_url = release.cover_source_url
        title = release.clean_title
        author = release.release_author

    # --- 1. Трекер (сеть, транзакция закрыта) ---
    if source_url:
        meta = None
        async with sem:
            await asyncio.sleep(settings.SCRAPE_DELAY_SEC)
            meta = await covers.fetch_url_to_cache(source_url)
        if meta is not None:
            _apply_cover_meta(release_id, meta, origin="tracker")
            return
        _bump_cover_retry(release_id)

    # --- 2. Интернет (сеть, транзакция закрыта) ---
    if not settings.COVER_SEARCH_ENABLED:
        return

    result = None
    async with sem:
        await asyncio.sleep(settings.SCRAPE_DELAY_SEC)
        log.info("release=%s: ищем обложку в интернете (%s — %s)",
                 release_id, author, title)
        try:
            result = await search_cover(title, author)
        except Exception:
            log.exception("release=%s: сбой поиска обложки", release_id)

    if result is None:
        log.warning("release=%s: обложка не найдена нигде", release_id)
        _bump_cover_retry(release_id)
        return

    meta = None
    async with sem:
        await asyncio.sleep(settings.SCRAPE_DELAY_SEC)
        try:
            meta = await covers.fetch_url_to_cache(result.url)
        except Exception:
            log.exception("release=%s: сбой скачивания обложки %s",
                          release_id, result.url)
            meta = None

    if meta is not None:
        _apply_cover_meta(release_id, meta, origin="online",
                          source=result.source, url=result.url)
        return

    _bump_cover_retry(release_id)


def _apply_cover_meta(release_id: int, meta: dict, *, origin: str,
                      source: str | None = None,
                      url: str | None = None) -> None:
    """Короткая транзакция: записывает скачанную обложку в карточку."""
    with _sync_session() as db:
        release = db.get(Release, release_id)
        if release is None:
            return
        release.cover_origin = origin
        release.cover_cache_path = meta["cache_path"]
        release.cover_sha256 = meta["sha256"]
        release.cover_mime = meta["mime"]
        release.cover_retry_count = 0
        if origin == "online":
            log.info("release=%s: обложка из %s (%s)",
                     release_id, source, url)


def _bump_cover_retry(release_id: int) -> None:
    """Короткая транзакция: увеличивает счётчик ретраев карточки."""
    with _sync_session() as db:
        release = db.get(Release, release_id)
        if release is None:
            return
        release.cover_retry_count = (release.cover_retry_count or 0) + 1


# ============================================================
# СЕТЕВЫЕ ХЕЛПЕРЫ
# ============================================================


async def _fast_download_detached(covers: CoverService, book_hash: str) -> None:
    """
    Быстрый старт загрузки книги БЕЗ удержания сессии БД во время await.

    Порядок (аналогично search_book_cover_detached в book_covers.py):
      1. Короткая транзакция: читаем книгу, извлекаем данные для сети.
      2. Сеть/торрент-клиент (без БД, сессия закрыта).
      3. Короткая транзакция: применяем результат (cover_meta).
      4. Короткая транзакция: выставляем приоритет первой главе.

    Никакой await внутри with _sync_session().
    """
    from app.catalog.cover_search import search_cover
    from app.db.models import Book, Chapter

    # ---------- 1. Короткая транзакция: читаем книгу ----------
    with _sync_session() as db:
        book = db.execute(
            select(Book).where(Book.book_hash == book_hash)
        ).scalars().first()
        if book is None:
            return

        if book.cover.is_settled:
            return

        release = book.releases[0] if book.releases else None
        if release is None:
            return

        cover_url = release.cover_source_url or None
        title = book.title or release.title or ""
        author = book.author or release.release_author or ""
        rubrics = [rp.rubric.title for rp in release.rubrics]
        release_id = release.id
        torrent_hash = release.torrent_hash

        first_chapter: Chapter | None = book.chapters[0] if book.chapters else None
        chapter_ready = (
            first_chapter is not None
            and first_chapter.status == ChapterStatus.READY.value
        )
        chapter_rel_path = first_chapter.rel_path if first_chapter else None
        chapter_file_index = first_chapter.file_index if first_chapter else None
        # сессия закрывается — все данные извлечены

    # ---------- 2. Сеть/торрент-клиент: БД НЕ залочена ----------
    cover_meta: dict | None = None

    # 2a. Обложка — прямой URL
    if cover_url:
        cover_meta = await covers.fetch_url_to_cache(cover_url)

    # 2b. Обложка — поиск в интернете
    if cover_meta is None and settings.COVER_SEARCH_ENABLED:
        try:
            result = await search_cover(title, author, rubrics)
            if result is not None:
                cover_meta = await covers.fetch_url_to_cache(result.url)
        except Exception:
            log.exception("book=%s: сбой поиска обложки", book_hash[:12])

    # ---------- 3. Короткая транзакция: сохраняем обложку ----------
    if cover_meta:
        with _sync_session() as db:
            book = db.get(Book, book_hash)
            if book is not None:
                book.cover.apply_meta(cover_meta, origin="online")
                book.cover_is_temp = False
                book.cover_retry_count = 0

    # ---------- 4. Приоритет первой главе (без БД) ----------
    if not chapter_ready and chapter_file_index is not None and torrent_hash:
        from app.torrents import manager as torrent_manager

        # file_index уже извлечён на шаге 1, вызываем клиент напрямую.
        # Никакой сессии БД — только HTTP к торрент-клиенту.
        try:
            ok = torrent_manager.set_file_priorities(
                torrent_hash,
                high=[chapter_file_index],
                normal=[],
                low=[],
            )
            if ok:
                log.debug("book=%s: приоритет главы #%d → 2",
                          book_hash[:12], chapter_file_index)
        except Exception:
            log.exception("book=%s: сбой приоритета главы", book_hash[:12])


async def _fetch_details(sem: asyncio.Semaphore,
                         source: TorrentSource,
                         url: str) -> "TorrentDetails | None":
    async with sem:
        await asyncio.sleep(settings.SCRAPE_DELAY_SEC)
        return await source.fetch_torrent_details(url)


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================


def _torrent_root_name(files, fallback: str) -> str:
    """Имя торрента — первый компонент rel_path первого файла."""
    if not files:
        return fallback
    parts = Path(files[0].name).parts
    return parts[0] if parts else fallback


def _prepare_files(files) -> list[dict]:
    """TorrentFileInfo → формат для structure.split_torrent_into_books."""
    files_sorted = sorted(files, key=lambda f: f.index)
    result = []
    offset = 0
    for f in files_sorted:
        result.append({
            "index": f.index,
            "rel_path": f.name,
            "size_bytes": f.size_bytes,
            "file_offset": offset,
        })
        offset += f.size_bytes
    return result


# ============================================================
# КЛАСС JOB
# ============================================================


class CatalogScanJob(Job):
    """
    Фоновая задача: обход страниц поиска торрент-трекера,
    создание/актуализация карточек раздач, загрузка обложек.
    """

    def __init__(self, source: TorrentSource, covers: CoverService) -> None:
        self._source = source
        self._covers = covers

    @property
    def name(self) -> str:
        return "sync_catalog"

    async def run(self) -> int:
        timeout = settings.SCRAPE_TOTAL_TIMEOUT_SEC
        try:
            result = await asyncio.wait_for(
                self._run_inner(),
                timeout=timeout,
            )
            log.info(">>> sync_catalog: завершено, обработано %d", result)
            return result
        except asyncio.TimeoutError:
            log.warning(">>> sync_catalog: прерван по таймауту %d сек", timeout)
            return 0

    async def _run_inner(self) -> int:
        source = self._source
        covers = self._covers
        sem = asyncio.Semaphore(max(1, settings.SCRAPE_DETAILS_CONCURRENCY))
        total = 0

        with _sync_session() as db:
            store = CrawlStateStore(db)
            state = store.load(source.name, source.base_url, "audiobooks")
            log.info(">>> sync_catalog: state = {oldest_seen=%s, resume_page=%s}",
                     state.oldest_seen_at, state.resume_page)

        def on_progress(page: int, st: CrawlState):
            with _sync_session() as db:
                s = db.merge(st)
                CrawlStateStore(db).save_progress(s)

        page_num = 0
        async for page in source.scan(
            query="mp3",
            state=state,
            on_progress=on_progress,
        ):
            log.info(">>> sync_catalog: страница %d (%d записей)",
                     page_num, len(page))

            for i, item in enumerate(page):
                try:
                    total += await _process_item(
                        source, covers, sem, item,
                    )
                    if (i + 1) % 10 == 0:
                        log.info(">>> sync_catalog: %d/%d, всего %d",
                                 i + 1, len(page), total)
                except Exception:
                    log.exception(">>> sync_catalog: ошибка %s",
                                  item.torrent_hash)

            page_num += 1

        with _sync_session() as db:
            CrawlStateStore(db).finish(db.merge(state))

        log.info(">>> sync_catalog: обход завершён, всего %d", total)
        return total