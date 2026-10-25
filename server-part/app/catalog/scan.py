"""
Сканирование структуры торрента и создание глав.

После получения метаданных торрента scan_book берёт карточку-раздачу книги
(book.release), получает список файлов из торрент-клиента и вызывает
ingest_release: разбиение на книги (app.torrents.structure), создание Book
с локальным ordinal, глав с локальным ordinal внутри книги, копирование
обложек томов и глобальное автоопределение серии.

Модель: Release = карточка раздачи (1..N книг), Book = книга (том),
Chapter привязан к книге (book_id) и раздаче (release_id).

Также здесь живёт update_chapter_from_alert — обновление главы по данным
торрент-клиента (backlog alert'ов).
"""
from __future__ import annotations


import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.catalog.covers import CoverService
from app.catalog.ingest import ingest_release
from app.db.models import Book, Chapter
from app.db.session import session_scope
from app.torrents import manager

log = logging.getLogger(__name__)


def scan_book(book_id: int, covers: CoverService) -> None:
    """
    После получения метаданных торрента:
      - берёт карточку-раздачу книги (book.release),
      - получает список файлов из торрент-клиента,
      - вызывает ingest_release: разбивает структуру на книги, создаёт
        книги и главы (локальный ordinal), копирует обложки томов.
    """
    with session_scope() as db:
        book = db.get(Book, book_id)
        if book is None:
            return

        release = book.release
        files = manager.get_files(release.torrent_hash)
        if not files:
            log.warning("book=%s: нет файлов для раздачи %s",
                        book_id, release.torrent_hash[:8])
            return

        torrent_name = _torrent_root_name(files, release.title)
        created = ingest_release(
            release_id=release.id,
            files=_prepare_files(files),
            torrent_name=torrent_name,
            covers=covers,
        )
        log.info("book=%s: создано глав: %d (release=%s)",
                 book_id, created, release.id)


def _torrent_root_name(files, fallback: str) -> str:
    """Имя торрента — первый компонент rel_path первого файла."""
    try:
        first = sorted(files, key=lambda f: f.index)[0]
        parts = Path(first.name).parts
        if parts:
            return parts[0]
    except Exception:
        pass
    return fallback


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
# ОБНОВЛЕНИЕ ГЛАВЫ ПО ALERT'У
# ============================================================

def recompute_book_status(db: Session, book_id: int) -> None:
    """Пересчитывает Book.status по статусам глав."""
    book = db.get(Book, book_id)
    if book is None:
        return
    book.recompute_status()


def update_chapter_from_alert(chapter_id: int, *,
                              status: str | None = None,
                              progress: float | None = None,
                              pieces_have: int | None = None,
                              downloaded_bytes: int | None = None) -> None:
    """Обновляет главу и каскадно пересчитывает статус книги."""
    with session_scope() as db:
        chapter = db.get(Chapter, chapter_id)
        if chapter is None:
            return
        if status is not None:
            chapter.status = status
        if progress is not None:
            chapter.progress = progress
        if pieces_have is not None:
            chapter.pieces_have = pieces_have
        if downloaded_bytes is not None:
            chapter.downloaded_bytes = downloaded_bytes
        chapter.last_modified_at = datetime.utcnow()
        if chapter.book is not None:
            chapter.book.recompute_status()