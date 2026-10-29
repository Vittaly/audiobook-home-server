import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.catalog import layout
from app.config import settings
from app.db.models import Book, BookStatus, Chapter, ChapterStatus
from app.db.session import session_scope
from app.torrents import manager

log = logging.getLogger(__name__)


def cleanup_unused() -> int:
    """
    Удаляет книги, все главы которых не стримились дольше TTL_DAYS.
    Работает по главам: смотрим chapter.last_streamed_at и book.last_accessed.
    Удаляем торрент целиком — libtorrent не умеет удалять отдельные файлы.
    """
    threshold = datetime.utcnow() - timedelta(days=settings.TTL_DAYS)
    removed = 0

    with session_scope() as db:
        candidates = db.execute(
            select(Book).where(Book.status.in_([
                BookStatus.READY.value,
                BookStatus.PARTIAL.value,
            ]))
        ).scalars().all()

        for book in candidates:
            last = book.last_accessed

            if last is not None and last >= threshold:
                continue

            # свежие главы?
            fresh = db.execute(
                select(Chapter.id).where(
                    Chapter.book_hash == book.book_hash,
                    Chapter.last_streamed_at.is_not(None),
                    Chapter.last_streamed_at >= threshold,
                ).limit(1)
            ).scalar_one_or_none()

            if fresh is not None:
                continue

            _remove_book(db, book)
            removed += 1

    log.info("cleanup_unused: удалено %d книг", removed)
    return removed


def _remove_book(db, book: Book) -> None:
    log.info("Удаляем book_hash=%s, last_accessed=%s",
             book.book_hash[:12], book.last_accessed)

    book.status = BookStatus.DELETING.value
    db.commit()

    # останавливаем торрент раздачи, которой принадлежит книга
    release = book.releases[0] if book.releases else None
    if release is not None:
        try:
            manager.remove(release.torrent_hash, delete_files=True)
        except Exception:
            log.exception("Не удалось удалить торрент %s",
                          release.torrent_hash)

    # папка торрента (общая для всех книг раздачи)
    _remove_release_folder(db, book)

    # главы
    chapters = db.execute(
        select(Chapter).where(Chapter.book_hash == book.book_hash)
    ).scalars().all()
    for chapter in chapters:
        chapter.status = ChapterStatus.DELETED.value
        chapter.downloaded_bytes = 0
        chapter.progress = 0.0
        chapter.pieces_have = 0

    book.status = BookStatus.DELETED.value
    if release is not None:
        release.recompute_status()


def _remove_release_folder(db, book: Book) -> None:
    """Удаляет папку торрента, если она осталась на диске."""
    if not book.chapters:
        return
    parts = Path(book.chapters[0].rel_path).parts
    if not parts:
        return
    folder = settings.downloads_dir / parts[0]
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
        log.info("book=%s: удалена папка %s", book.book_hash[:12], folder)

    # главы
    chapters = db.execute(
        select(Chapter).where(Chapter.book_hash == book.book_hash)
    ).scalars().all()
    for chapter in chapters:
        chapter.status = ChapterStatus.DELETED.value
        chapter.downloaded_bytes = 0
        chapter.progress = 0.0
        chapter.pieces_have = 0

    book.status = BookStatus.DELETED.value


def cleanup_orphans() -> int:
    """
    При старте: папки в downloads/ без активных торрентов → rmtree.
    """
    downloads_dir = settings.downloads_dir
    if not downloads_dir.exists():
        return 0

    # активные хэши
    active_hashes: set[str] = set()
    with session_scope() as db:
        books_with_release = db.execute(
            select(Book).where(
                Book.status.in_([
                    BookStatus.DOWNLOADING.value,
                    BookStatus.PARTIAL.value,
                    BookStatus.READY.value,
                ])
            )
        ).scalars().all()
        for book in books_with_release:
            release = book.releases[0] if book.releases else None
            if release and release.torrent_hash:
                active_hashes.add(release.torrent_hash)

    removed = 0
    for folder in downloads_dir.iterdir():
        if not folder.is_dir():
            continue
        # проверяем, не связана ли папка с раздачей
        folder_clean = folder.name.strip()
        if folder_clean in active_hashes:
            continue  # ещё нужна

        # Если папка не связана ни с одной известной раздачей —
        # смотрим, не занята ли она торрент-клиентом
        torrent = manager.get_torrent(folder_clean)
        if torrent is not None:
            continue

        shutil.rmtree(folder, ignore_errors=True)
        log.info("cleanup_orphans: удалена папка %s", folder)
        removed += 1

    return removed