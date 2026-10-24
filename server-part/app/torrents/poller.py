"""
Поллер синхронизации торрентов с БД.

Раз в TRANSMISSION_POLL_SEC секунд:
  1. Получает список активных торрентов из клиента.
  2. Для каждого находит Release (карточка раздачи).
  3. Если у раздачи ещё нет глав (ни у одной книги) и метаданные готовы —
     создаёт книги и главы (ingest_release).
  4. Актуализирует раздачу через actualize_release(): догоняет статусы глав
     и приводит приоритеты к целевой двухфазной схеме.
  5. Помечает раздачи, чьи торренты пропали из клиента, как announced
     (статусы книг пересчитываются, карточка агрегируется).

Правило продвижения high (см. actualize_release):
  Приоритет в торрент-клиенте задаётся НА ТОРРЕНТ, а один торрент — это вся
  раздача (все тома). Поэтому high считается ПО ВСЕЙ раздаче:
    books по ordinal -> chapters по ordinal.
  Фаза 1 — качается ровно первая глава раздачи.
  Фаза 2 (есть готовая глава) — качается вся раздача, high на первую
  незагруженную главу, остальные аудио и обложки в normal.
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.book_covers import search_book_cover_detached
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

# последний применённый приоритет по раздаче: hash -> (file_index, фаза2).
# Защита от повторного дёргания торрент-клиента на каждом проходе, пока
# целевая глава и фаза не изменились.
_last_priority: dict[str, tuple[int, bool]] = {}


# ============================================================
# ПУБЛИЧНАЯ ТОЧКА ВХОДА
# ============================================================

async def poll_downloads(covers: CoverService) -> dict:
    """Один проход синхронизации. Возвращает статистику."""
    if not manager.ping():
        # причина уже прологирована внутри manager.ping() (с троттлингом),
        # чтобы не спамить одним и тем же предупреждением каждые 5 сек
        log.debug(">>> poller: торрент-клиент недоступен")
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

            if not created:
                return None, []

            log.info("release=%s: создано %d глав из торрента %s",
                     release.id, created, t.hash[:8])
            search_ids = [
                b.id for b in release.books
                if _book_needs_online_cover(b)
            ]
            # при создании глав все absent — actualize_release сам поставит
            # приоритеты (фаза 1: качается ровно первая глава раздачи)
            actualize_release(db, t.hash, release)
            return "scanned", search_ids

        # ---------- главы есть, актуализируем раздачу ----------
        # статусы глав + приоритеты всей раздачи — теми же действиями,
        # что и в эндпоинте запроса главы
        changed = actualize_release(db, t.hash, release)

        # попутно докачиваем/копируем обложки томов и собираем книги,
        # которым нужен онлайн-поиск обложки
        search_ids: list[int] = []
        for book in release.books:
            if _try_copy_book_cover(book, covers):
                changed = True
            if _book_needs_online_cover(book):
                search_ids.append(book.id)

        if changed:
            log.debug("release=%s: статусы глав обновлены", release.id)
            return "updated", search_ids

        return None, search_ids


def _release_has_chapters(release: Release) -> bool:
    return any(b.chapters for b in release.books)


# ============================================================
# ЯДРО АКТУАЛИЗАЦИИ РАЗДАЧИ
# ============================================================

def actualize_release(db: Session, torrent_hash: str,
                      release: Release) -> bool:
    """
    Приводит раздачу к целевой двухфазной схеме приоритетов.

    Единая функция актуализации: её вызывают и очередной проход поллера
    (см. _process_torrent), и эндпоинт запроса главы
    (см. api/streaming.sync_chapter_status). Обе точки выполняют ровно одни
    и те же действия — логика не разъезжается.

    1. Догоняет статусы глав по данным торрента (по всем книгам раздачи).
    2. Определяет фазу:
       - фаза 1 — ни одна глава раздачи ещё не готова: качается ровно первая
         глава раздачи, всё прочее в skip;
       - фаза 2 — есть хотя бы одна готовая глава: качается вся раздача,
         high на первую незагруженную главу, остальные аудио и обложки
         томов в normal.
    3. Приоритеты — один вызов set_file_priorities по всей раздаче.

    Возвращает True, если какие-то статусы глав изменились.
    """
    files = manager.get_files(torrent_hash)
    if not files:
        return False

    files_by_idx = {f.index: f for f in files}

    # ---- 1. Догнать статусы глав ----
    changed = False
    for book in release.books:
        if _update_chapters(book, files_by_idx):
            changed = True

    # ---- 2. Первая незагруженная глава ПО ВСЕЙ РАЗДАЧЕ ----
    ordered = _ordered_chapters(release)
    if not ordered:
        return changed

    first_pending = next(
        (c for c in ordered if c.status != "ready"), None)
    if first_pending is None:
        log.info("release=%s: все главы раздачи скачаны, "
                 "приоритеты не меняем", release.id)
        return changed

    # фаза 2 начинается, как только хоть одна глава раздачи готова
    phase2 = any(c.status == "ready" for c in ordered)

    log.info("release=%s: %s, high → ordinal=%d file_index=%d (status=%s)",
             release.id, "фаза 2" if phase2 else "фаза 1",
             first_pending.ordinal, first_pending.file_index,
             first_pending.status)

    # ---- 3. Приоритеты всей раздачи одним вызовом ----
    _apply_priorities(torrent_hash, files, first_pending.file_index, phase2)
    return changed


def _ordered_chapters(release: Release) -> list[Chapter]:
    """Главы всей раздачи в целевом порядке: books по ordinal -> chapters."""
    result: list[Chapter] = []
    for book in sorted(release.books, key=lambda b: b.ordinal):
        result.extend(sorted(book.chapters, key=lambda c: c.ordinal))
    return result


# ============================================================
# ОБНОВЛЕНИЕ СТАТУСОВ
# ============================================================

def _update_chapters(book: Book, files_by_idx: dict) -> bool:
    """Догоняет статусы глав книги по данным торрента.

    files_by_idx — {file_index: TorrentFileInfo} по всей раздаче.
    """
    changed = False

    for chapter in book.chapters:
        f = files_by_idx.get(chapter.file_index)
        if f is None:
            continue

        progress = float(f.progress)

        if f.is_complete or progress >= 1.0:
            if chapter.status != "ready":
                chapter.mark_ready()
                changed = True

        elif progress > 0:
            if chapter.status in ("absent", "downloading") or \
                    abs(chapter.progress - progress) > 0.01:
                chapter.mark_progress(progress)
                changed = True

    if changed:
        book.recompute_status()
        release = book.release
        if release is not None:
            release.recompute_status()

    return changed


# ============================================================
# ПРИОРИТЕТЫ
# ============================================================

def _apply_priorities(torrent_hash: str, files,
                      high_file_index: int, phase2: bool) -> None:
    """Выставляет приоритеты по всей раздаче одним вызовом.

    Фаза 1: high на первую главу раздачи; остальные аудио, обложки и прочий
            мусор — skip (качается ровно одна глава).
    Фаза 2: high на первую незагруженную главу; остальные аудио раздачи и
            обложки томов — normal; прочий не-аудио мусор — skip.

    Обложки (картинки) держим в normal и в фазе 1, и в фазе 2 — иначе файл
    обложки тома не скачается и книга останется с временной обложкой.
    Sequential выключен: порядок файлов раздачи не равен аудио-порядку глав,
    порядок задают приоритеты.
    """
    audio_ids = [f.index for f in files if _is_audio(f.name)]
    if high_file_index not in audio_ids:
        log.warning(">>> poller: file_index=%s не найден среди аудио %s",
                    high_file_index, torrent_hash[:8])
        return

    image_ids = [f.index for f in files if is_image(f.name)]
    other_ids = [f.index for f in files
                 if not _is_audio(f.name) and not is_image(f.name)]

    high = [high_file_index]
    if phase2:
        normal = [i for i in audio_ids if i != high_file_index] + image_ids
        low = other_ids
    else:
        normal = list(image_ids)
        low = [i for i in audio_ids if i != high_file_index] + other_ids

    # идемпотентность: если цель и фаза не изменились — не дёргаем клиент
    key = (high_file_index, phase2)
    if _last_priority.get(torrent_hash) == key:
        return

    ok = manager.set_file_priorities(
        torrent_hash, high=high, normal=normal, low=low)
    if ok:
        _last_priority[torrent_hash] = key

    manager.enable_sequential(torrent_hash, enabled=False)


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

    Поиск по книге имеет смысл только для многотомных раздач: у однократных
    нет своей обложки тома в принципе, и их обложкой занимается уровень
    раздачи (_ensure_cover: трекер → интернет).
    """
    if book.cover_cache_path is not None or not book.cover_is_temp:
        return False

    release = book.release
    if release is None or len(release.books) <= 1:
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

    Использует detached-поиск: БД транзакция открывается только на время
    коротких чтения/записи, сеть (поиск + скачивание) проходит без
    блокировки базы — иначе поллер/API падают с "database is locked".
    """
    try:
        await search_book_cover_detached(covers, book_id)
    except Exception:
        log.exception("book=%s: фоновая ошибка поиска обложки", book_id)
    finally:
        _search_in_flight.discard(book_id)
