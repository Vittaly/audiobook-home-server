"""
Доменные операции загрузки книги.

Всё, что значит «загрузить книгу» — здесь: проверки состояния,
выбор save_path, обращение к торрент-клиенту, обновление БД,
фиксация активности. Роут только вызывает эти функции.

Модель: Release = карточка раздачи (1..N книг). Торрент добавляется
в клиент целиком (manager.start_download(release, ...)) — он общий для
всех книг раздачи. Статусы книг агрегируются из глав; статус карточки
агрегируется из статусов книг (poller обновляет их по факту загрузки).

Клиент торрента (transmission/libtorrent) не упоминается —
взаимодействие идёт через app.torrents.manager.
"""
from __future__ import annotations


import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.catalog import layout, service
from app.config import settings
from app.db.models import Book, BookStatus, ChapterStatus, Release
from app.lifecycle import activity
from app.torrents import manager

log = logging.getLogger(__name__)


# ============================================================
# РЕЗУЛЬТАТ
# ============================================================

@dataclass
class DownloadResult:
    """Результат операции запуска загрузки."""
    success: bool
    # Идентификатор раздачи — основной объект загрузки; книги (одна или
    # несколько) — производные от раздачи, их может ещё не быть на старте.
    release_id: int | None = None
    book_id: int | None = None
    client_id: int | None = None
    status: str | None = None
    save_path: str | None = None
    error_code: str | None = None       # 'not_found' | 'no_release' | ...
    error_message: str | None = None


# ============================================================
# START
# ============================================================

def _apply_temp_cover(book: Book) -> None:
    """
    При старте загрузки книги без своей обложки включаем признак временной
    обложки: пока торрент качается и своя обложка не получена (файл докачан
    или найден онлайн), клиент показывает обложку карточки-раздачи —
    осмысленную картинку, а не заглушку.
    """
    if book.cover_cache_path:
        book.cover_is_temp = False
        return
    book.cover_is_temp = True


async def start(db: Session, book_id: int) -> DownloadResult:
    """
    Запускает загрузку книги. Возвращает DownloadResult.
    Роут маппит error_code на HTTP-код.

    Торрент добавляется на уровне карточки-раздачи (release) —
    клиент не умеет качать отдельные файлы выборочно на старте.
    """
    book = service.get_book(db, book_id)
    if book is None:
        return DownloadResult(
            success=False, error_code="not_found",
            error_message="book not found")

    release = book.release
    if release is None:
        return DownloadResult(
            success=False, error_code="no_release",
            error_message="у книги нет раздачи-источника")

    if not release.magnet and not release.source_url:
        return DownloadResult(
            success=False, error_code="no_release",
            error_message="у раздачи нет ни magnet, ни source_url")

    if book.status == BookStatus.READY.value:
        return DownloadResult(
            success=False, release_id=release.id,
            error_code="already_done",
            error_message="книга уже скачана")

    # торрент уже активен в клиенте — считается, что качается
    torrent = manager.get_torrent(release.torrent_hash)
    if torrent is not None:
        log.debug("book=%s: торрент раздачи %s уже в клиенте",
                  book_id, release.torrent_hash[:8])
        book.status = BookStatus.DOWNLOADING.value
        release.status = BookStatus.DOWNLOADING.value
        _apply_temp_cover(book)
        activity.touch(db, book.id, "download")
        db.commit()
        # save_path и client_id берём из фактического торрента в клиенте —
        # он уже добавлен, его путь и хэш зафиксированы
        save_path = str(torrent.save_path) if getattr(torrent, "save_path", None) \
            else str(settings.downloads_dir)
        return DownloadResult(
            success=True, release_id=release.id, book_id=book.id,
            status=book.status,
            client_id=release.torrent_hash,
            save_path=save_path,
            error_code="already_downloading",
            error_message="торрент уже добавлен в клиент")

    free_mb = layout.free_space_mb()
    if free_mb < settings.MIN_FREE_DISK_MB:
        log.warning("book=%s: недостаточно места: %d МБ < %d МБ",
                    book_id, free_mb, settings.MIN_FREE_DISK_MB)
        return DownloadResult(
            success=False, release_id=release.id,
            error_code="no_space",
            error_message=f"недостаточно места на диске: {free_mb} МБ")

    save_path = settings.downloads_dir

    try:
        client_id = await manager.start_download(release, save_path)
    except Exception as e:
        # Защита от непредвиденных ошибок клиента/сети (например,
        # UnicodeEncodeError при построении заголовков). Возвращаем
        # чистый JSON-ответ вместо креша ASGI (HTTP 500 без тела).
        log.exception("book=%s: ошибка запуска загрузки", book_id)
        return DownloadResult(
            success=False, release_id=release.id,
            error_code="client_failed",
            error_message=f"ошибка запуска загрузки: {e}")

    if client_id is None:
        return DownloadResult(
            success=False, release_id=release.id,
            error_code="client_failed",
            error_message="торрент-клиент отказал в добавлении")

    # статусы выставятся/уточнятся поллером по факту появления файлов;
    # сейчас даём клиенту быстрый отклик: карточка и книга — downloading
    book.status = BookStatus.DOWNLOADING.value
    release.status = BookStatus.DOWNLOADING.value
    _apply_temp_cover(book)
    activity.touch(db, book.id, "download")
    db.commit()

    log.info("book=%s: старт загрузки раздачи %s, client_id=%s",
             book_id, release.torrent_hash[:8], client_id)

    return DownloadResult(
        success=True,
        release_id=release.id,
        book_id=book.id,
        client_id=client_id,
        status=book.status,
        save_path=str(save_path),
    )


async def start_release(db: Session, release_id: int,
                        book_id: int | None = None) -> DownloadResult:
    """
    Запускает загрузку всей раздачи на уровне карточки (release).

    Книги до старта загрузки на сервере нет (раздачи создаются без
    книги-заглушки) — поэтому клиент стартует торрент по id раздачи,
    а уже появившиеся книги поллит из каталога.

    book_id — опциональный «впечатлённый» id книги (для первой книги
    раздачи, если она уже успела появиться): используется только для
    активности и отдачи в ответе, сам торрент стоит на уровне раздачи.
    """
    release = service.get_release(db, release_id)
    if release is None:
        return DownloadResult(
            success=False, error_code="not_found",
            error_message="release not found")

    if not release.magnet and not release.source_url:
        return DownloadResult(
            success=False, release_id=release_id,
            error_code="no_release",
            error_message="у раздачи нет ни magnet, ни source_url")

    # торрент уже активен в клиенте — считается, что качается
    torrent = manager.get_torrent(release.torrent_hash)
    if torrent is not None:
        log.debug("release=%s: торрент %s уже в клиенте",
                  release_id, release.torrent_hash[:8])
        release.status = BookStatus.DOWNLOADING.value
        for b in release.books:
            b.status = BookStatus.DOWNLOADING.value
            _apply_temp_cover(b)
        target_id = book_id or (release.books[0].id if release.books else None)
        if target_id is not None:
            activity.touch(db, target_id, "download")
        db.commit()
        save_path = str(torrent.save_path) if getattr(torrent, "save_path", None) \
            else str(settings.downloads_dir)
        return DownloadResult(
            success=True, release_id=release_id, book_id=target_id,
            status=release.status,
            client_id=release.torrent_hash,
            save_path=save_path,
            error_code="already_downloading",
            error_message="торрент уже добавлен в клиент")

    free_mb = layout.free_space_mb()
    if free_mb < settings.MIN_FREE_DISK_MB:
        log.warning("release=%s: недостаточно места: %d МБ < %d МБ",
                    release_id, free_mb, settings.MIN_FREE_DISK_MB)
        return DownloadResult(
            success=False, release_id=release_id,
            error_code="no_space",
            error_message=f"недостаточно места на диске: {free_mb} МБ")

    save_path = settings.downloads_dir

    try:
        client_id = await manager.start_download(release, save_path)
    except Exception as e:
        # См. комментарий в start(): не даём исключению дойти до ASGI.
        log.exception("release=%s: ошибка запуска загрузки", release_id)
        return DownloadResult(
            success=False, release_id=release_id,
            error_code="client_failed",
            error_message=f"ошибка запуска загрузки: {e}")

    if client_id is None:
        return DownloadResult(
            success=False, release_id=release_id,
            error_code="client_failed",
            error_message="торрент-клиент отказал в добавлении")

    # статусы выставятся/уточнятся поллером по факту появления файлов;
    # сейчас даём клиенту быстрый отклик: карточка — downloading
    release.status = BookStatus.DOWNLOADING.value
    for b in release.books:
        b.status = BookStatus.DOWNLOADING.value
        _apply_temp_cover(b)
    target_id = book_id or (release.books[0].id if release.books else None)
    if target_id is not None:
        activity.touch(db, target_id, "download")
    db.commit()

    log.info("release=%s: старт загрузки раздачи, client_id=%s",
             release_id, client_id)

    return DownloadResult(
        success=True,
        release_id=release_id,
        book_id=target_id,
        client_id=client_id,
        status=release.status,
        save_path=str(save_path),
    )


# ============================================================
# STOP
# ============================================================

def stop(db: Session, book_id: int,
         delete_files: bool = False) -> DownloadResult:
    """Останавливает загрузку. Файлы опционально удаляются."""
    book = service.get_book(db, book_id)
    if book is None:
        return DownloadResult(
            success=False, error_code="not_found",
            error_message="book not found")

    release = book.release
    if release is None:
        return DownloadResult(
            success=False, error_code="no_release",
            error_message="у книги нет раздачи-источника")

    ok = manager.remove(release.torrent_hash, delete_files=delete_files)
    if not ok:
        log.warning("book=%s: торрент %s не найден в клиенте",
                    book.id, release.torrent_hash[:8])

    if delete_files:
        # файлы общие для всех книг раздачи — сбрасываем их тоже
        _reset_release_chapters(db, release, status=ChapterStatus.ABSENT.value)
        for b in release.books:
            b.recompute_status()
        release.recompute_status()
    else:
        # файлы оставляем; статусы уточнит poller по пропавшему торренту
        pass

    db.commit()

    return DownloadResult(
        success=True, book_id=book.id, status=book.status)


# ============================================================
# DELETE
# ============================================================

def remove(db: Session, book_id: int) -> DownloadResult:
    """Удаляет торрент с файлами. Метаданные сохраняются."""
    book = service.get_book(db, book_id)
    if book is None:
        return DownloadResult(
            success=False, error_code="not_found",
            error_message="book not found")

    release = book.release
    if release is None:
        return DownloadResult(
            success=False, error_code="no_release",
            error_message="у книги нет раздачи-источника")

    removed = manager.remove(release.torrent_hash, delete_files=True)
    log.info("book=%s: удалён торрент раздачи %s (ok=%s)",
             book.id, release.torrent_hash[:8], removed)

    _remove_release_folder(release)

    _reset_release_chapters(db, release, status=ChapterStatus.DELETED.value)
    for b in release.books:
        b.recompute_status()
    release.recompute_status()

    activity.touch(db, book.id, "delete")
    db.commit()

    return DownloadResult(
        success=True, book_id=book.id, status=book.status)


# ============================================================
# ВНУТРЕННЕЕ
# ============================================================

def _reset_release_chapters(db: Session, release: Release,
                            *, status: str) -> None:
    """Сбрасывает прогресс всех глав всех книг раздачи."""
    for b in release.books:
        b.downloaded_at = None
        for chapter in b.chapters:
            is_deleted = (status == ChapterStatus.DELETED.value)
            chapter.status = status
            chapter.progress = 0.0
            chapter.downloaded_bytes = 0
            chapter.pieces_have = 0
            if not is_deleted:
                chapter.downloaded_bytes = 0


def _release_torrent_folder(release: Release) -> Path | None:
    """Корневая папка торрента внутри downloads_dir (из первой главы)."""
    for b in release.books:
        for chapter in b.chapters:
            parts = Path(chapter.rel_path).parts
            if parts:
                return settings.downloads_dir / parts[0]
    return None


def _remove_release_folder(release: Release) -> None:
    """Удаляет папку торрента, если она осталась на диске."""
    folder = _release_torrent_folder(release)
    if folder is None or not folder.exists():
        return
    shutil.rmtree(folder, ignore_errors=True)
    log.info("release=%s: удалена папка %s", release.id, folder)