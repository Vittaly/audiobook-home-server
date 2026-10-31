"""
Job: синхронизация состояния торрент-клиента с БД.

Раз в TRANSMISSION_POLL_SEC секунд:
  1. Получает список активных торрентов из клиента.
  2. Для каждого находит Release и передаёт файлы в release.actualize_download_files().
  3. Если торрент пропал из клиента — release.mark_all_download_files_absent().

Поллер — это мост между торрент-клиентом и Release.
Он только передаёт состояние файлов; всё остальное (каскад статусов,
копирование обложек, пересчёт приоритетов) — ответственность моделей.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.catalog.book_covers import search_book_cover_detached
from app.catalog.covers import CoverService
from app.config import settings
from app.db.models import Book, Release
from app.db.session import session_scope
from app.scheduler.base import Job
from app.torrents import manager

log = logging.getLogger(__name__)

# book_hash книг, для которых уже запущен фоновый онлайн-поиск обложки
_search_in_flight: set[str] = set()


class PollDownloadsJob(Job):
    """
    Фоновая задача: синхронизация состояния торрент-клиента с БД.
    """

    def __init__(self, covers: CoverService) -> None:
        self._covers = covers

    @property
    def name(self) -> str:
        return "poll_downloads"

    async def run(self) -> dict:
        if not manager.ping():
            log.debug(">>> poll_downloads: торрент-клиент недоступен")
            return {"error": "client unavailable"}

        active = manager.list_active()
        active_hashes = {t.hash.lower() for t in active}

        log.debug(">>> poll_downloads: активных торрентов %d", len(active))

        updated = 0
        search_ids: list[str] = []

        for t in active:
            try:
                # _process_torrent возвращает (changed, search_ids) или None
                result = await self._process_torrent(t)
                if result is not None:
                    changed, ids = result
                    search_ids.extend(ids)
                    if changed:
                        updated += 1
            except Exception:
                log.exception(">>> poll_downloads: ошибка обработки торрента %s",
                              t.hash[:8])

        # фоновый онлайн-поиск обложек книг (не блокирует этот проход)
        for book_hash in search_ids:
            if book_hash in _search_in_flight:
                continue
            _search_in_flight.add(book_hash)
            asyncio.create_task(self._run_book_cover_search(book_hash))

        # пропавшие торренты
        missing = self._handle_missing(active_hashes)

        log.debug(">>> poll_downloads: updated=%d, missing=%d, "
                  "cover_search_scheduled=%d",
                  updated, missing, len(search_ids))

        return {
            "active": len(active),
            "updated": updated,
            "missing": missing,
            "cover_search_scheduled": len(search_ids),
        }

    async def _process_torrent(self, t) -> tuple[bool, list[str]] | None:
        """
        Обрабатывает один торрент: актуализирует статусы DownloadFile.

        Returns:
            (changed, search_ids) кортеж:
                changed    — True если хотя бы один DownloadFile изменил статус
                             или была скопирована обложка тома
                search_ids — список book_hash для онлайн-поиска обложки
            None если релиз не наш.
        """
        with session_scope() as db:
            release = db.execute(
                select(Release).where(Release.torrent_hash == t.hash)
            ).scalar_one_or_none()
            if release is None:
                log.debug(">>> poll_downloads: торрент %s не наш, пропуск",
                          t.hash[:8])
                return None

            files = manager.get_files(t.hash)
            if not files:
                return (False, [])

            # Актуализируем DownloadFile по данным клиента
            changed = release.actualize_download_files(files)

            # копирование обложек томов с диска в кэш (если файл готов)
            search_ids: list[str] = []
            for book in release.books:
                if self._try_copy_book_cover(book):
                    changed = True
                if book.needs_online_cover:
                    search_ids.append(book.book_hash)

            if changed:
                db.commit()
                log.debug("release=%s: статусы обновлены через DownloadFile",
                          release.id)
            else:
                db.commit()

            return (changed, search_ids)

    def _try_copy_book_cover(self, book: Book) -> bool:
        """
        Если файл картинки тома из торрента докачался на диск — копирует его
        в кэш обложек и снимает cover_is_temp. Возвращает True при изменении.
        """
        if book.cover_cache_path is not None or not book.cover_path_hint:
            return False

        hint_path = settings.downloads_dir / book.cover_path_hint
        if not hint_path.exists():
            return False

        try:
            ok = self._covers.cache_from_torrent(book, hint_path)
        except Exception:
            log.exception("book=%s: сбой копирования обложки %s",
                          book.book_hash[:12], hint_path)
            return False

        if not ok:
            log.warning("book=%s: файл %s не скопирован как обложка",
                        book.book_hash[:12], hint_path)
            return False

        book.cover_is_temp = False
        log.info("book=%s: обложка тома скопирована из %s",
                 book.book_hash[:12], hint_path)
        return True

    async def _run_book_cover_search(self, book_hash: str) -> None:
        """
        Фоновая задача онлайн-поиска обложки книги.
        """
        try:
            await search_book_cover_detached(self._covers, book_hash)
        except Exception:
            log.exception("book=%s: фоновая ошибка поиска обложки",
                          book_hash[:12])
        finally:
            _search_in_flight.discard(book_hash)

    def _handle_missing(self, active_hashes: set[str]) -> int:
        """
        Помечает раздачи, чьи торренты пропали из клиента.

        Передаёт пустой список в mark_all_download_files_absent() —
        все файлы загрузки получают статус ABSENT, каскад делает остальное.
        """
        count = 0

        with session_scope() as db:
            rows = db.execute(
                select(Release).where(
                    Release.status.in_(["downloading", "partial"])
                )
            ).scalars().all()

            for release in rows:
                if release.torrent_hash.lower() in active_hashes:
                    continue

                log.warning("release=%s: торрент %s пропал из клиента",
                            release.id, release.torrent_hash[:8])

                if release.mark_all_download_files_absent():
                    db.commit()
                count += 1

        if count:
            log.info(">>> poll_downloads: обработано пропавших торрентов %d",
                     count)

        return count