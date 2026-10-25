"""
Логика отдачи аудио-файлов с поддержкой Range.

Используется роутом /books/{id}/stream/{chapter_id}.
Отдаёт только полностью скачанные главы (status='ready').

Путь к файлу резолвится так:
  1. Спрашиваем Transmission через manager.get_files() — он знает
     точный abs_path, независимо от того, как назвал папку торрент
     и что лежит в chapter.rel_path.
  2. Если Transmission недоступен или файл не найден —
     fallback на settings.downloads_dir / chapter.rel_path.

При любом отказе (409, 410) логирует подробную диагностику.
"""
from __future__ import annotations


import logging
import mimetypes
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Chapter, ChapterStatus

log = logging.getLogger(__name__)


# ============================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================================

def serve_chapter(chapter: Chapter, range_header: str | None) -> Response:
    """
    Отдаёт главу как файл с поддержкой Range.

    :param chapter: Chapter из БД (с уже подгруженным release)
    :param range_header: содержимое заголовка Range (может быть None)
    """
    # ---- 1. Статусные проверки ----
    if chapter.status == ChapterStatus.DELETED.value:
        _log_reject(chapter, "410 deleted", range_header=range_header)
        raise HTTPException(410, "глава удалена")

    if chapter.status != ChapterStatus.READY.value:
        _log_reject(chapter, f"409 not ready (status={chapter.status})",
                    range_header=range_header)
        raise HTTPException(
            409,
            f"глава ещё не скачана (status={chapter.status})",
        )

    # ---- 2. Ищем файл ----
    abs_path = _resolve_file_path(chapter)

    if abs_path is None or not abs_path.exists():
        _log_reject(chapter, "410 file missing",
                    file_exists=abs_path.exists() if abs_path else False,
                    range_header=range_header)
        raise HTTPException(410, "файл главы отсутствует на диске")

    # ---- 3. Отдаём ----
    media_type = _guess_media_type(abs_path)
    etag = f'"chapter-{chapter.id}-{chapter.size_bytes}"'

    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Cache-Control": "public, max-age=86400",
    }

    log.debug(
        "stream OK: chapter=%s book=%s status=%s path=%r size=%s range=%s",
        chapter.id, chapter.book_id, chapter.status, str(abs_path),
        abs_path.stat().st_size, range_header,
    )

    return FileResponse(
        abs_path,
        media_type=media_type,
        headers=headers,
        filename=abs_path.name,
    )


# ============================================================
# СИНХРОННАЯ ПЕРЕПРОВЕРКА СТАТУСА
# ============================================================

def sync_chapter_status(chapter: Chapter, db: Session) -> Chapter:
    """
    Синхронно актуализирует раздачу по запросу главы.

    Вызывается из stream-роута ДО serve_chapter, когда статус в БД ещё не
    'ready'. Поллер обновляет статусы раз в несколько секунд, и плеер (или
    очередь докачки) может стучаться в эндпоинт раньше, чем поллер заметит
    завершение файла. Здесь мы не ждём следующего прохода поллера, а сразу
    выполняем ровно те же действия, что и проход поллера, — вызываем общую
    актуализацию раздачи actualize_release() (догон статусов глав +
    приведение приоритетов всей раздачи к целевой схеме).

    Возвращает (возможно, обновлённый) chapter.
    """
    if chapter.status == ChapterStatus.READY.value:
        return chapter

    release = chapter.release
    if release is None or not release.torrent_hash:
        return chapter

    try:
        from app.torrents.poller import actualize_release

        was_status = chapter.status
        changed = actualize_release(db, release.torrent_hash, release)

        if changed:
            db.commit()
            db.refresh(chapter)

        if chapter.status != was_status:
            log.info(
                "stream: chapter=%s актуализирован: %s → %s, book=%s",
                chapter.id, was_status, chapter.status, chapter.book_id,
            )
    except Exception:
        log.exception(
            "stream: ошибка актуализации chapter=%s, "
            "оставляем статус %s",
            chapter.id, chapter.status,
        )

    return chapter


# ============================================================
# РЕЗОЛВИНГ ПУТИ
# ============================================================

def _resolve_file_path(chapter: Chapter) -> Path | None:
    """
    Возвращает абсолютный путь к файлу главы.

    1. Пробуем через Transmission — берём f.abs_path по file_index.
    2. Fallback — settings.downloads_dir / chapter.rel_path.

    Ничего не создаёт, не меняет. Только читает.
    """
    # ---- попытка через Transmission ----
    if chapter.release is not None:
        try:
            from app.torrents import manager as torrents_manager

            files = torrents_manager.get_files(chapter.release.torrent_hash)
            target = next(
                (f for f in files if f.index == chapter.file_index),
                None,
            )
            if target is not None and target.abs_path is not None:
                log.debug(
                    "resolved path via transmission: chapter=%s → %s",
                    chapter.id, target.abs_path,
                )
                return Path(target.abs_path)

            log.debug(
                "chapter=%s: file_index=%s не найден в торренте %s, "
                "используем fallback",
                chapter.id, chapter.file_index,
                chapter.release.torrent_hash[:8],
            )
        except Exception:
            log.exception(
                "chapter=%s: ошибка резолвинга через Transmission, "
                "используем fallback", chapter.id,
            )

    # ---- fallback ----
    fallback = settings.downloads_dir / chapter.rel_path
    log.debug(
        "resolved path via fallback: chapter=%s → %s",
        chapter.id, fallback,
    )
    return fallback


# ============================================================
# ЛОГ ОТКАЗА
# ============================================================

def _log_reject(chapter: Chapter,
                reason: str,
                *,
                file_exists: bool | None = None,
                file_size: int | None = None,
                range_header: str | None = None) -> None:
    """
    Пишет в лог всё, что нужно для разбора отказа.
    Один WARNING на запрос — сжато, но с максимумом контекста.
    """
    torrent_info = "-"
    file_info = "-"

    try:
        release = chapter.release
        if release is not None:
            from app.torrents import manager as torrents_manager

            t = torrents_manager.get_torrent(release.torrent_hash)
            if t is None:
                torrent_info = (
                    f"hash={release.torrent_hash[:8]} "
                    f"не найден в клиенте"
                )
            else:
                torrent_info = (
                    f"hash={release.torrent_hash[:8]} "
                    f"state={t.status} progress={t.progress:.2%} "
                    f"peers={t.peers} seeds={t.seeds} "
                    f"rate_dl={t.rate_download} B/s"
                )

            files = torrents_manager.get_files(release.torrent_hash)
            if files:
                target = next(
                    (f for f in files if f.index == chapter.file_index),
                    None,
                )
                if target is None:
                    file_info = (
                        f"file_index={chapter.file_index} НЕ найден "
                        f"среди {len(files)} файлов клиента"
                    )
                else:
                    file_info = (
                        f"file_index={chapter.file_index} "
                        f"progress={target.progress:.2%} "
                        f"size={target.size_bytes} "
                        f"is_complete={target.is_complete} "
                        f"abs_path={target.abs_path} "
                        f"name={target.name[:60]!r}"
                    )
    except Exception:
        log.exception(
            "stream reject: ошибка сбора диагностики для chapter=%s",
            chapter.id,
        )

    log.warning(
        "stream reject [%s]: "
        "chapter=%s book=%s ordinal=%s file_index=%s "
        "db_status=%s db_progress=%.2f db_size=%s "
        "rel_path=%r "
        "disk_exists=%s disk_size=%s "
        "torrent=[%s] file=[%s] "
        "range=%s",
        reason,
        chapter.id, chapter.book_id, chapter.ordinal, chapter.file_index,
        chapter.status, chapter.progress, chapter.size_bytes,
        chapter.rel_path,
        file_exists, file_size,
        torrent_info, file_info,
        range_header,
    )


# ============================================================
# MIME
# ============================================================

def _guess_media_type(path: Path) -> str:
    ext = path.suffix.lower()
    table = {
        ".mp3":  "audio/mpeg",
        ".m4a":  "audio/mp4",
        ".m4b":  "audio/mp4",
        ".aac":  "audio/aac",
        ".flac": "audio/flac",
        ".ogg":  "audio/ogg",
        ".opus": "audio/ogg",
        ".wav":  "audio/wav",
    }
    if ext in table:
        return table[ext]

    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"