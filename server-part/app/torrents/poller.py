"""
Поллер синхронизации торрентов с БД.

Раз в TRANSMISSION_POLL_SEC секунд:
  1. Получает список активных торрентов из клиента.
  2. Для каждого находит Release (карточка раздачи).
  3. Если у раздачи ещё нет глав (ни у одной книги) и метаданные готовы —
     создаёт книги и главы (ingest_release) и ставит high на первую
     нескачанную главу первой книги.
  4. Если главы есть — обновляет статусы по книгам. Когда какая-то глава
     завершилась, продвигает high на первую нескачанную главу этой книги:
       - пропускаем всё, что уже ready,
       - на первой не-ready ставим priority_high.
  5. Помечает раздачи, чьи торренты пропали из клиента, как announced
     (статусы книг пересчитываются, карточка агрегируется).

Правило продвижения high:
  high всегда на первой по ordinal нескаченной главе книги.
  sequential download включён — файлы книги качаются по порядку.
"""

import asyncio
import logging

from sqlalchemy import select

from app.catalog.book_covers import try_search_book_cover
from app.catalog.covers import CoverService
from app.catalog.ingest import ingest_release
from app.config import settings
from app.db.models import Book, Chapter, Release
from app.db.session import session_scope
from app.torrents import manager
from app.torrents.utils import is_image

log = logging.getLogger(__name__)

_AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".opus"}

# id книг, для которых уже запущен фоновый онлайн-поиск обложки
_search_in_flight: set[int] = set()


# ============================================================
# ПУБЛИЧНАЯ ТОЧКА ВХОДА
# ============================================================

async def poll_downloads(covers: CoverService) -> dict:
    """Один проход синхронизации. Возвращает статистику."""
    if not manager.ping():
        log.warning(">>> poller: торрент-клиент недоступен")
        return {"error": "client unavailable"}

    active = manager.list_active()
    active_hashes = {t.hash.lower() for t in active}

    log.debug(">>> poller: активных торрентов %d", len(active))

    scanned = 0
    updated = 0
    search_ids: list[int] = []

    for t in active:
        try:
            result, pending = _process_torrent(t, covers)
            search_ids.extend(pending)
            if result == "scanned":
                scanned += 1
            elif result == "updated":
                updated += 1
        except Exception:
            log.exception(">>> poller: ошибка обработки торрента %s",
                          t.hash[:8])

    # фоновый онлайн-поиск обложек книг (не блокирует этот проход)
    for book_id in search_ids:
        if book_id in _search_in_flight:
            continue
        _search_in_flight.add(book_id)
        asyncio.create_task(_run_book_cover_search(covers, book_id))

    missing = _handle_missing(active_hashes)

    return {
        "active": len(active),
        "scanned": scanned,
        "updated": updated,
        "missing": missing,
        "cover_search_scheduled": len(search_ids),
    }


# ============================================================
# ОБРАБОТКА ОДНОГО ТОРРЕНТА
# ============================================================

def _process_torrent(t, covers: CoverService) -> tuple[str | None, list[int]]:
    """
    Обрабатывает один торрент.

    Возвращает (результат, список id книг, которым нужен онлайн-поиск обложки).
    """
    with session_scope() as db:
        release = db.execute(
            select(Release).where(Release.torrent_hash == t.hash)
        ).scalar_one_or_none()
        if release is None:
            log.debug(">>> poller: торрент %s не наш, пропуск", t.hash[:8])
            return None, []

        # ---------- главы ещё не созданы ----------
        if not _release_has_chapters(release):
            files = manager.get_files(t.hash)
            if not files:
                return None, []

            created = ingest_release(
                release_id=release.id,
                files=_prepare_files(files),
                torrent_name=t.name,
                covers=covers,
            )

            if created:
                log.info("release=%s: создано %d глав из торрента %s",
                         release.id, created, t.hash[:8])
                # при создании глав все absent →
                # high на первую главу первой книги
                first = release.books[0] if release.books else None
                if first is not None and first.chapters:
                    _advance_high(t.hash, first)
                search_ids = [
                    b.id for b in release.books
                    if _book_needs_online_cover(b)
                ]
                return "scanned", search_ids

            return None, []

        # ---------- главы есть, обновляем статусы ----------
        files = manager.get_files(t.hash)
        if not files:
            return None, []

        # обновляем только те книги, файлы которых есть в группе;
        # попутно докачиваем/копируем обложки томов и собираем книги,
        # которым нужен онлайн-поиск обложки
        changed = False
        search_ids: list[int] = []
        for book in release.books:
            if book.chapters:
                changed |= _update_chapters(t.hash, book, files)
            changed |= _try_copy_book_cover(book, covers)
            if _book_needs_online_cover(book):
                search_ids.append(book.id)

        if changed:
            log.debug("release=%s: статусы глав обновлены", release.id)
            return "updated", search_ids

        return None, search_ids


def _release_has_chapters(release: Release) -> bool:
    return any(b.chapters for b in release.books)


# ============================================================
# ОБНОВЛЕНИЕ СТАТУСОВ
# ============================================================

def _update_chapters(torrent_hash: str, book: Book, files) -> bool:
    """
    Обновляет статусы глав книги по данным из торрента.
    Когда что-то стало ready — продвигает high на первую нескаченную.
    """
    chapters_by_idx = {c.file_index: c for c in book.chapters}

    changed = False
    any_became_ready = False

    for f in files:
        chapter = chapters_by_idx.get(f.index)
        if chapter is None:
            continue

        progress = float(f.progress)

        if f.is_complete or progress >= 1.0:
            if chapter.status != "ready":
                chapter.mark_ready()
                changed = True
                any_became_ready = True

        elif progress > 0:
            if chapter.status in ("absent", "downloading") or \
                    abs(chapter.progress - progress) > 0.01:
                chapter.mark_progress(progress)
                changed = True

    if changed:
        book.recompute_status()

    # переставляем high только когда что-то завершилось
    if any_became_ready:
        _advance_high(torrent_hash, book)

    return changed


# ============================================================
# ПРИОРИТЕТЫ
# ============================================================

def _advance_high(torrent_hash: str, book: Book) -> None:
    """
    Ставит high на первую по ordinal нескаченную главу книги.

    Логика:
      - идём по главам в порядке ordinal;
      - всё, что ready, пропускаем;
      - первая с status != 'ready' получает priority_high;
      - остальные аудио — normal, sequential on.

    Если все главы ready — ничего не делаем.
    """
    sorted_chapters = sorted(book.chapters, key=lambda c: c.ordinal)

    first_pending: Chapter | None = None
    for c in sorted_chapters:
        if c.status != "ready":
            first_pending = c
            break

    if first_pending is None:
        log.info("book=%s: все главы скачаны, приоритеты не меняем",
                 book.id)
        return

    log.info("book=%s: high → ordinal=%d file_index=%d (status=%s)",
             book.id, first_pending.ordinal, first_pending.file_index,
             first_pending.status)

    _apply_high(torrent_hash, first_pending.file_index)


def _apply_high(torrent_hash: str, high_file_index: int) -> None:
    """
    Ставит high на указанный файл, normal на остальные аудио и файлы
    обложек томов. Включает sequential download.
    """
    files = manager.get_files(torrent_hash)
    if not files:
        return

    audio_ids = [f.index for f in files if _is_audio(f.name)]
    if not audio_ids or high_file_index not in audio_ids:
        log.warning(">>> poller: file_index=%s не найден среди аудио %s",
                    high_file_index, torrent_hash[:8])
        return

    # обложки томов качаем в normal, чтобы после загрузки книги
    # файл картинки уже был на диске (cover_is_temp → своя обложка)
    image_ids = [f.index for f in files if is_image(f.name)]

    high = [high_file_index]
    normal = [i for i in audio_ids if i != high_file_index] + image_ids

    manager.set_file_priorities(
        torrent_hash, high=high, normal=normal, low=[])
    manager.enable_sequential(torrent_hash, enabled=True)


# ============================================================
# КОНВЕРТАЦИЯ
# ============================================================

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
# ПРОПАВШИЕ ТОРРЕНТЫ
# ============================================================

def _handle_missing(active_hashes: set[str]) -> int:
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

            for book in release.books:
                if not book.chapters:
                    book.status = "announced"
                    book.error_message = None
                else:
                    had_progress = False
                    for c in book.chapters:
                        if c.status in ("downloading", "partial"):
                            c.status = "absent"
                            c.progress = 0.0
                            c.downloaded_bytes = 0
                            had_progress = True
                    if had_progress:
                        book.recompute_status()
                release.recompute_status()
            count += 1

    if count:
        log.info(">>> poller: обработано пропавших торрентов %d", count)

    return count


# ============================================================
# ХЕЛПЕРЫ
# ============================================================

def _is_audio(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(ext) for ext in _AUDIO_EXTS)


# ============================================================
# ОБЛОЖКИ ТОМОВ (докачка + онлайн-поиск)
# ============================================================

def _try_copy_book_cover(book: Book, covers: CoverService) -> bool:
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
        ok = covers.cache_from_torrent(book, hint_path)
    except Exception:
        log.exception("book=%s: сбой копирования обложки %s",
                      book.id, hint_path)
        return False

    if not ok:
        log.warning("book=%s: файл %s не скопирован как обложка",
                    book.id, hint_path)
        return False

    book.cover_is_temp = False
    log.info("book=%s: обложка тома скопирована из %s", book.id, hint_path)
    return True


def _book_needs_online_cover(book: Book) -> bool:
    """
    Нужен ли книге онлайн-поиск обложки в этом проходе.

    Да, когда у книги временная обложка, нет своей, ещё не исчерпан лимит
    ретраев, и файл-картинка из торрента либо не заявлен вовсе (нет hint),
    либо заявлен, но книга уже готова, а файла так и не появилось —
    значит его нет в раздаче, ищем онлайн.
    """
    if book.cover_cache_path is not None or not book.cover_is_temp:
        return False

    if (book.cover_retry_count or 0) >= settings.COVER_RETRY_LIMIT:
        return False

    if book.cover_path_hint:
        if book.status != "ready":
            # ждём, пока файл картинки докачается вместе с главой
            return False
        hint_path = settings.downloads_dir / book.cover_path_hint
        if hint_path.exists():
            return False
        # книга готова, а файла картинки нет — его нет в раздаче

    return True


async def _run_book_cover_search(covers: CoverService, book_id: int) -> None:
    """
    Фоновая задача онлайн-поиска обложки книги.

    Открывает собственную сессию (объекты из сессии поллера уже закрыты),
    повторно проверяет актуальность и коммитит результат через session_scope.
    """
    try:
        with session_scope() as db:
            book = db.execute(
                select(Book).where(Book.id == book_id)
            ).scalar_one_or_none()
            if book is None:
                return

            # повторная проверка: обложка могла появиться (файл докачался,
            # другой проход поллера уже снял temp)
            if book.cover_cache_path is not None or not book.cover_is_temp:
                return

            await try_search_book_cover(covers, book, book.release)
            # session_scope закоммитит cover_* при выходе
    except Exception:
        log.exception("book=%s: фоновая ошибка поиска обложки", book_id)
    finally:
        _search_in_flight.discard(book_id)
